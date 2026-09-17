"""Deterministic X (x.com) signup state machine (Task 11).

A known form → a deterministic Playwright choreography, NOT per-click LLM control
(so escalation states are testable). Every unrecognized/blocking page classifies
into an :class:`Obstacle` and RAISES :class:`SignupPaused` — the caller
(tools/x_browser/escalation.py) hands it to the owner. Nothing here defeats a
CAPTCHA.

Invariants:
- ONE account per tenant: a stored ``x`` session refuses a new signup.
- The generated password is stored (encrypted) BEFORE it is ever typed.
- Progress persists after every completed step under the ``x_signup`` provider,
  so a paused signup resumes at the recorded step.
- The account bio carries an automation disclosure.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Ordered signup steps. Progress is the last COMPLETED step.
STEPS = ("open", "details", "request_code", "wait_code", "enter_code",
         "password", "profile", "export")


_CODE_RE = None  # compiled lazily to avoid a module-import regex cost


class MailPoller:
    """Extract the X verification code from the agent's own inbox.

    Wraps an AgentMail-style client (``list_messages``/``get_message``) and scans
    recent mail from an x.com sender for a 6-digit code.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def wait_for_code(self, timeout: int = 120) -> Optional[str]:
        import re
        global _CODE_RE
        if _CODE_RE is None:
            _CODE_RE = re.compile(r"\b(\d{6})\b")
        try:
            summaries = await self.client.list_messages(limit=10)
        except Exception as e:
            logger.debug("x signup mail list failed: %s", e)
            return None
        for summary in summaries:
            mid = summary.get("message_id")
            if not mid:
                continue
            try:
                msg = await self.client.get_message(mid)
            except Exception:
                continue
            sender = str(msg.get("from") or "").lower()
            if "x.com" not in sender and "twitter" not in sender:
                continue
            body = str(msg.get("extracted_text") or msg.get("text") or "")
            m = _CODE_RE.search(body)
            if m:
                return m.group(1)
        return None


class Obstacle(Enum):
    VALUE_NEEDED = "value_needed"
    INTERACTIVE_CHALLENGE = "interactive_challenge"
    PHONE_REQUIRED = "phone_required"
    BLOCKED = "blocked"


class SignupPaused(Exception):
    """Signup stopped on an obstacle; the owner must act."""

    def __init__(self, obstacle: Obstacle, prompt: str,
                 resume_token: Optional[str] = None) -> None:
        super().__init__(f"{obstacle.value}: {prompt}")
        self.obstacle = obstacle
        self.prompt = prompt
        self.resume_token = resume_token


@dataclass
class SignupResult:
    handle: str
    address: str
    #: What the post-signup profile step actually did. ``requested_handle`` is
    #: what we asked for; ``handle`` above is what X shows. ``bio_applied``
    #: False means the automation disclosure is NOT on the profile yet and the
    #: owner must add it by hand — say so, never imply compliance.
    requested_handle: str = ""
    handle_applied: bool = False
    bio_applied: bool = False


def classify_page(state: str) -> Optional[Obstacle]:
    """Map a driver probe string to an obstacle (or None to proceed).

    An UNKNOWN state is treated as an interactive challenge — the safe default
    is to hand an unrecognized wall to a human, never to guess past it.
    """
    s = (state or "").lower()
    if any(k in s for k in ("arkose", "captcha", "challenge", "verify you", "unusual")):
        return Obstacle.INTERACTIVE_CHALLENGE
    if "phone" in s:
        return Obstacle.PHONE_REQUIRED
    if any(k in s for k in ("suspend", "blocked", "denied", "restricted")):
        return Obstacle.BLOCKED
    if s in ("ok", "home", "next", ""):
        return None
    return Obstacle.INTERACTIVE_CHALLENGE


class SignupFlow:
    def __init__(self, *, driver: Any, mail: Any, store: Any, progress: Any,
                 identity: Any, user_id: str) -> None:
        self.driver = driver
        self.mail = mail
        self.store = store
        self.progress = progress
        self.identity = identity
        self.user_id = user_id

    # -- helpers ----------------------------------------------------------

    async def _guard_page(self) -> None:
        """Probe the page; raise SignupPaused on any obstacle after persisting."""
        obstacle = classify_page(await self.driver.probe())
        if obstacle is None:
            return
        await self._persist(self._current_step)
        prompt = {
            Obstacle.INTERACTIVE_CHALLENGE:
                "X is showing a challenge (likely a CAPTCHA). Solve it in the "
                "open browser window, or resume the signup locally.",
            Obstacle.PHONE_REQUIRED:
                "X wants a phone number to continue. Provide one or abort.",
            Obstacle.BLOCKED:
                "X blocked/suspended the signup. Not retrying.",
            Obstacle.VALUE_NEEDED:
                "X needs a value to continue.",
        }[obstacle]
        raise SignupPaused(obstacle, prompt, resume_token=self.user_id)

    async def _persist(self, step: str) -> None:
        try:
            state = await self.driver.export_storage_state()
        except Exception:
            state = {}
        self.progress.save(self.user_id,
                           extra={"signup_step": step, "signup_state": state})

    def _resume_step(self) -> Optional[str]:
        rec = self.progress.load(self.user_id) or {}
        return rec.get("signup_step")

    # -- run --------------------------------------------------------------

    async def run(self, resume: bool = False) -> SignupResult:
        # One account per tenant — but only a COMPLETED session (storage_state
        # present) blocks a new signup. A password-only partial record from an
        # interrupted attempt must NOT trip the guard, or resume can never run.
        existing_rec = self.store.load(self.user_id)
        if existing_rec is not None and existing_rec.get("storage_state") is not None:
            existing = existing_rec.get("handle", "unknown")
            raise SignupPaused(
                Obstacle.BLOCKED,
                f"an X account (@{existing}) already exists for this agent — "
                "one account per instance. Delete it first to re-register.")

        start_index = 0
        if resume:
            last = self._resume_step()
            if last in STEPS:
                start_index = STEPS.index(last) + 1

        email = self._agent_email()
        if not email:
            # Filling an EMPTY email into the form fails several steps later
            # with a misleading "no verification code" pause. Refuse here and
            # name the remedy instead.
            raise SignupPaused(
                Obstacle.VALUE_NEEDED,
                "the agent has no email address to register with. Set "
                "AGENTMAIL_API_KEY (the agent provisions its own inbox) or "
                "POLYROB_AGENT_EMAIL, then retry.")
        if getattr(self.mail, "client", object()) is None:
            raise SignupPaused(
                Obstacle.VALUE_NEEDED,
                "no inbox client to read X's verification code from — set "
                "AGENTMAIL_API_KEY, then retry.")
        password = self._ensure_password()
        self._profile_outcome = {}

        for step in STEPS[start_index:]:
            self._current_step = step
            await self._run_step(step, email, password)
            await self._guard_page()
            await self._persist(step)

        live_handle = await self.driver.current_handle()
        self.store.save(self.user_id,
                        storage_state=await self.driver.export_storage_state(),
                        password=password,
                        handle=live_handle)
        self.progress.delete(self.user_id)
        outcome = getattr(self, "_profile_outcome", None) or {}
        return SignupResult(
            handle=live_handle, address=email,
            requested_handle=str(getattr(self.identity, "handle", "") or ""),
            handle_applied=bool(outcome.get("handle_applied")),
            bio_applied=bool(outcome.get("bio_applied")))

    async def _run_step(self, step: str, email: str, password: str) -> None:
        if step == "open":
            await self.driver.open_signup()
        elif step == "details":
            await self.driver.fill_details(
                self.identity.name, email, self.identity.dob)
        elif step == "request_code":
            await self.driver.request_code()
        elif step == "wait_code":
            self._code = await self._await_code()
        elif step == "enter_code":
            await self.driver.enter_code(getattr(self, "_code", "") or
                                         await self._await_code())
        elif step == "password":
            await self.driver.set_password(password)
        elif step == "profile":
            outcome = await self.driver.set_handle_and_profile(
                self.identity.handle, self.identity.disclosure)
            self._profile_outcome = dict(outcome or {})
        elif step == "export":
            # storage_state captured by the caller after the loop.
            pass

    async def _await_code(self, tries: int = 20) -> str:
        for _ in range(tries):
            code = await self.mail.wait_for_code(timeout=120)
            if code:
                return code
        raise SignupPaused(
            Obstacle.VALUE_NEEDED,
            "no verification code arrived in the agent's inbox — provide the "
            "code X sent, or check the mailbox.")

    def _agent_email(self) -> str:
        from core.instance import resolve_agent_email
        return resolve_agent_email() or ""

    def _ensure_password(self) -> str:
        """Generate + store the password BEFORE it is ever typed into the page."""
        rec = self.progress.load(self.user_id) or {}
        pw = rec.get("password")
        if not pw:
            pw = secrets.token_urlsafe(24)
            self.store.save(self.user_id, password=pw)
            self.progress.save(self.user_id, password=pw)
        return pw
