"""x_browser — use X through a real, owner-captured browser session.

⚠️ This module deliberately does NOT ``from __future__ import annotations``: the
Registry introspects each action's first-param annotation to route the validated
Pydantic model, and stringized annotations break that (see the shared landmine in
AGENTS.md / the action-registration modules).

Dedicated verbs only (post, DM read/send, login check, signup) — never
raw browser clicks — so a name-based approval gate can actually distinguish
"post to X" from "click a cookie banner". Capabilities high_impact +
delegate_blocked; ``x_post`` is owner-approval-gated and ``x_signup_start`` is
always-gated (owner-queued even under autonomous mode).
"""
import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from core.config import BotConfig
from core.rate_limit import SlidingWindowLimiter
from tools.base_tool import BaseTool
from tools.controller.types import ActionResult

logger = logging.getLogger(__name__)

X_ALLOWED_DOMAINS = ["x.com", "twitter.com", "api.twitter.com"]


class XPostAction(BaseModel):
    """Publish one post (tweet) to X from the agent's saved session."""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=280,
                      description="The post body (<=280 chars).")


class XReplyAction(BaseModel):
    """Reply to an existing X post from the agent's saved session.

    2026-09-19: the X API tier answers 403 to a reply aimed at anyone who has
    not mentioned us, which killed the outreach programme's Phase-1
    "value-first replies" lane outright. The browser rail exists to bypass
    exactly that tier and had no reply verb — so the lane stayed dead even for
    a captured session. ``in_reply_to`` accepts the status URL or the bare id.
    """
    model_config = ConfigDict(extra="forbid")
    in_reply_to: str = Field(
        ..., description="The post to reply under: its x.com/…/status/<id> URL "
                         "or the bare numeric status id.")
    text: str = Field(..., min_length=1, max_length=280,
                      description="The reply body (<=280 chars).")

    @property
    def status_id(self) -> str:
        return _status_id_from(self.in_reply_to)

    @classmethod
    def _validate_in_reply_to(cls, value: str) -> str:
        if not _status_id_from(value):
            raise ValueError(
                "in_reply_to must be an x.com status URL (…/status/<id>) or a "
                "numeric status id")
        return value

    def model_post_init(self, __context) -> None:  # pydantic v2 hook
        self._validate_in_reply_to(self.in_reply_to)


def _status_id_from(value: str) -> str:
    """The numeric status id inside a status URL or a bare id; ``""`` if neither."""
    import re
    v = (value or "").strip()
    m = re.search(r"/status/(\d+)", v)
    if m:
        return m.group(1)
    return v if v.isdigit() else ""


class XLoginCheckAction(BaseModel):
    """No parameters. An explicit empty model (like TwitterWhoamiAction) so the
    registry never has to auto-generate one — that fallback logged a WARNING at
    every session start (106/24h on prod)."""
    model_config = ConfigDict(extra="forbid")


class XSignupStartAction(BaseModel):
    """Begin (or resume) the agent's own X account signup ceremony."""
    model_config = ConfigDict(extra="forbid")
    handle_hint: Optional[str] = Field(
        None, description="Preferred @handle (X may require a variant).")
    resume: bool = Field(
        False, description="Resume a previously paused signup.")


class XReadDMsAction(BaseModel):
    """Read the visible X inbox or one existing conversation."""
    model_config = ConfigDict(extra="forbid")
    participant: Optional[str] = Field(
        None, description="Existing thread to open: @handle, visible name, or "
                          "conversation id. Omit to list the visible inbox.")
    max_results: int = Field(20, ge=1, le=100,
                             description="Maximum rows/messages to return.")


class XDMAction(BaseModel):
    """Send a DM in an existing visible X conversation."""
    model_config = ConfigDict(extra="forbid")
    participant: str = Field(
        ..., min_length=1, description="Existing thread: @handle, visible name, "
                                       "or conversation id.")
    text: str = Field(..., min_length=1, max_length=10000,
                      description="Direct-message text.")


def _leaf_or_forged(execution_context) -> bool:
    """True for a delegated/leaf/forged turn — never let one drive X."""
    if execution_context is None:
        return False
    if getattr(execution_context, "is_sub_agent", False):
        return True
    if getattr(execution_context, "role", "orchestrator") == "leaf":
        return True
    md = getattr(execution_context, "metadata", None) or {}
    return md.get("turn_kind") in ("self_wake", "delegation_result")


class XBrowserTool(BaseTool):
    """Browser-based X posting + account registration on a saved login."""

    _WRITE_WINDOW_SEC = 3600.0

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        super().__init__(name=name, config=config, container=container)
        self._session_store = None
        from core.env import int_env
        self._post_limiter = SlidingWindowLimiter(
            max_calls=int_env("TWITTER_WRITE_MAX_PER_HOUR", 15),
            window_seconds=self._WRITE_WINDOW_SEC)

    async def _initialize(self) -> None:
        # Nothing to validate at init — a missing session is reported per-verb
        # with a remedy, not a hard init failure.
        return None

    # -- session + browser plumbing --------------------------------------

    @property
    def session_store(self):
        if self._session_store is None:
            from tools.x_browser.session_store import XSessionStore
            self._session_store = XSessionStore()
        return self._session_store

    def _user_id(self, execution_context) -> str:
        uid = getattr(execution_context, "user_id", None) or ""
        if uid:
            return uid
        from core.identity import resolve_identity
        return resolve_identity()

    async def _open_driver(self, user_id: str):
        """Open a dedicated X-scoped browser context on the saved session.

        Returns ``(driver, release)`` where ``release`` is an async-safe cleanup
        callable. Overridden in tests to inject a fake driver.
        """
        record = self.session_store.load(user_id)
        storage_state = (record or {}).get("storage_state") or None
        if self.container is None:
            raise RuntimeError("no container — browser unavailable")
        bm = self.container.get_service("browser_manager")
        if bm is None:
            raise RuntimeError("browser_manager not available")
        if not getattr(bm, "is_initialized", False):
            await bm.initialize()
        browser = await bm.get_browser()
        from tools.browser.context import BrowserContextConfig
        cfg = BrowserContextConfig(
            allowed_domains=X_ALLOWED_DOMAINS, storage_state=storage_state)
        ctx = await browser.new_context(config=cfg, session_id=f"xbrowser_{user_id}")
        session = await ctx.get_session()
        from tools.x_browser.driver import XPageDriver
        driver = XPageDriver(session.current_page)

        async def _release():
            try:
                await ctx.close()
            except Exception as e:
                logger.debug("x context close failed: %s", e)
        return driver, _release

    async def _run_release(self, release) -> None:
        if release is None:
            return
        res = release()
        if hasattr(res, "__await__"):
            await res

    # -- actions ---------------------------------------------------------

    @BaseTool.action(
        "Publish a post to X (x.com) from the agent's own saved account. "
        "Owner-approval-gated. Requires a captured X session.",
        param_model=XPostAction,
    )
    async def x_post(self, params: XPostAction, execution_context=None) -> ActionResult:
        if _leaf_or_forged(execution_context):
            return ActionResult(
                error="x_post is blocked for delegated/forged turns — report back "
                      "and let the main agent post.",
                include_in_memory=True)
        user_id = self._user_id(execution_context)
        if not self.session_store.exists(user_id):
            return ActionResult(
                error="no X session — run `polyrob x-account capture-session` "
                      "(owner login) "
                      "or `x_signup_start` to create the agent's account first.",
                include_in_memory=True)
        if not self._post_limiter.check(user_id):
            return ActionResult(
                error="hourly X post cap reached (TWITTER_WRITE_MAX_PER_HOUR).",
                include_in_memory=True)
        try:
            driver, release = await self._open_driver(user_id)
        except Exception as e:
            return ActionResult(error=f"could not open X session: {e}",
                                include_in_memory=True)
        try:
            url = await driver.post(params.text)
        except Exception as e:
            return ActionResult(
                error=f"post failed: {e} — the session may be expired; "
                      "run `polyrob x-account capture-session` to refresh it.",
                include_in_memory=True)
        finally:
            await self._run_release(release)
        return ActionResult(extracted_content=f"posted to X: {url}",
                            include_in_memory=True)

    @BaseTool.action(
        "Reply to an existing X post (by status URL or id) from the agent's own "
        "saved account — the public-reply lane the API tier refuses (403) for "
        "non-mentioners. Owner-approval-gated. Requires a captured X session.",
        param_model=XReplyAction,
    )
    async def x_reply(self, params: XReplyAction, execution_context=None) -> ActionResult:
        if _leaf_or_forged(execution_context):
            return ActionResult(
                error="x_reply is blocked for delegated/forged turns — report back "
                      "and let the main agent reply.",
                include_in_memory=True)
        user_id = self._user_id(execution_context)
        if not self.session_store.exists(user_id):
            return ActionResult(
                error="no X session — run `polyrob x-account capture-session` "
                      "(owner login) first; until then public replies have no "
                      "working rail (the API tier returns 403).",
                include_in_memory=True)
        if not self._post_limiter.check(user_id):
            return ActionResult(
                error="hourly X post cap reached (TWITTER_WRITE_MAX_PER_HOUR).",
                include_in_memory=True)
        try:
            driver, release = await self._open_driver(user_id)
        except Exception as e:
            return ActionResult(error=f"could not open X session: {e}",
                                include_in_memory=True)
        try:
            url = await driver.reply(params.status_id, params.text)
        except Exception as e:
            return ActionResult(
                error=f"reply failed: {e} — the session may be expired; "
                      "run `polyrob x-account capture-session` to refresh it.",
                include_in_memory=True)
        finally:
            await self._run_release(release)
        return ActionResult(
            extracted_content=f"replied on X under status {params.status_id}: {url}",
            include_in_memory=True)

    @BaseTool.action(
        "Read the inbox visible in the agent's logged-in X browser session, or "
        "read one existing thread. Use this when X API DM reads are empty or "
        "own-only; browser results reflect the inbox UI and include inbound replies.",
        param_model=XReadDMsAction,
    )
    async def x_read_dms(self, params: XReadDMsAction,
                         execution_context=None) -> ActionResult:
        user_id = self._user_id(execution_context)
        if not self.session_store.exists(user_id):
            return ActionResult(
                error="no X session — run `polyrob x-account capture-session` "
                      "on a machine with a visible browser.",
                include_in_memory=True)
        try:
            driver, release = await self._open_driver(user_id)
        except Exception as e:
            return ActionResult(error=f"could not open X session: {e}",
                                include_in_memory=True)
        try:
            result = await driver.read_dms(
                params.participant or "", params.max_results)
        except Exception as e:
            return ActionResult(
                error=f"browser DM read failed: {e} — run `polyrob x-account "
                      "capture-session` if the login expired.",
                include_in_memory=True)
        finally:
            await self._run_release(release)
        import json
        return ActionResult(
            extracted_content="X browser DM read (visible inbox):\n" +
                              json.dumps(result, ensure_ascii=False, indent=2),
            include_in_memory=True)

    @BaseTool.action(
        "Send a direct message in an existing X conversation through the saved "
        "browser session. Owner-approval-gated.",
        param_model=XDMAction,
    )
    async def x_dm(self, params: XDMAction,
                   execution_context=None) -> ActionResult:
        if _leaf_or_forged(execution_context):
            return ActionResult(
                error="x_dm is blocked for delegated/forged turns — report back "
                      "and let the main agent send it.",
                include_in_memory=True)
        user_id = self._user_id(execution_context)
        if not self.session_store.exists(user_id):
            return ActionResult(
                error="no X session — run `polyrob x-account capture-session` "
                      "on a machine with a visible browser.",
                include_in_memory=True)
        if not self._post_limiter.check((user_id, "dm")):
            return ActionResult(
                error="hourly X browser write cap reached "
                      "(TWITTER_WRITE_MAX_PER_HOUR).",
                include_in_memory=True)
        try:
            driver, release = await self._open_driver(user_id)
        except Exception as e:
            return ActionResult(error=f"could not open X session: {e}",
                                include_in_memory=True)
        try:
            result = await driver.send_dm(params.participant, params.text)
        except Exception as e:
            return ActionResult(
                error=f"browser DM send failed: {e} — run `polyrob x-account "
                      "capture-session` if the login expired.",
                include_in_memory=True)
        finally:
            await self._run_release(release)
        return ActionResult(
            extracted_content=("DM sent through X browser conversation "
                               f"{result.get('conversation_id', '')}"),
            include_in_memory=True)

    @BaseTool.action(
        "Register a NEW X (x.com) account for the agent through the browser. "
        "Runs autonomously and pings the owner only on an obstacle (CAPTCHA, "
        "phone check). Always owner-approval-gated. One account per agent.",
        param_model=XSignupStartAction,
    )
    async def x_signup_start(self, params: XSignupStartAction,
                             execution_context=None) -> ActionResult:
        if _leaf_or_forged(execution_context):
            return ActionResult(
                error="x_signup_start is blocked for delegated/forged turns.",
                include_in_memory=True)
        user_id = self._user_id(execution_context)
        session_id = getattr(execution_context, "session_id", None) or ""
        # Bounded escalation loop: run → on pause, escalate → if cleared, resume.
        from tools.x_browser.signup import SignupPaused
        resume = params.resume
        for _ in range(4):
            try:
                flow = await self._build_signup_flow(user_id, resume)
                result = await flow.run(resume=resume)
            except SignupPaused as paused:
                outcome = await self._escalate(
                    paused, user_id, session_id,
                    driver=getattr(self, "_signup_driver", None))
                from tools.x_browser.escalation import EscalationOutcome
                if outcome is EscalationOutcome.CLEARED:
                    resume = True
                    continue
                if outcome is EscalationOutcome.ABORTED:
                    return ActionResult(
                        error=f"X signup stopped: {paused.prompt}",
                        include_in_memory=True)
                return ActionResult(
                    extracted_content=(
                        "X signup paused — the owner has been asked to help: "
                        f"{paused.prompt}. It will resume once handled."),
                    include_in_memory=True)
            except Exception as e:
                return ActionResult(error=f"X signup failed: {e}",
                                    include_in_memory=True)
            return ActionResult(
                extracted_content=self._signup_summary(result),
                include_in_memory=True)
        return ActionResult(
            extracted_content="X signup made progress but is still paused after "
                              "several owner checkpoints — resume later.",
            include_in_memory=True)

    @staticmethod
    def _signup_summary(result) -> str:
        """One honest line per fact: the live handle, whether it is the one we
        asked for, and whether the automation disclosure reached the bio. A
        missing disclosure is named as owner work — X's automation rules want
        it on the profile, and implying it is there when it is not is the
        confident-wrong shape this tree refuses everywhere else."""
        parts = [f"X account created: @{result.handle} ({result.address})."]
        req = getattr(result, "requested_handle", "") or ""
        if req and not getattr(result, "handle_applied", False) \
                and req.lower() != (result.handle or "").lower():
            parts.append(f"Requested handle @{req} was NOT applied (taken or refused) — "
                         f"X kept @{result.handle}; change it at x.com/settings/screen_name.")
        if getattr(result, "bio_applied", False):
            parts.append("Automation disclosure written to the bio.")
        else:
            parts.append("⚠️ Automation disclosure NOT applied — add it to the bio by hand "
                         "at x.com/settings/profile before posting.")
        parts.append("Session stored; post with x_post.")
        return " ".join(parts)

    async def _build_signup_flow(self, user_id: str, resume: bool):
        """Construct a live SignupFlow (headless on a server). Overridable in tests."""
        from core.instance import resolve_instance_id
        from tools.x_browser.session_store import XSessionStore
        from tools.x_browser.signup import MailPoller, SignupFlow

        client = self._agentmail_client()
        if client is None:
            # Refuse BEFORE a browser is opened: the flow cannot read X's
            # verification code without the agent's own inbox.
            raise RuntimeError(
                "X signup needs the agent's inbox to receive the verification "
                "code — set AGENTMAIL_API_KEY (the agent provisions its own "
                "address) and retry.")
        # The inbox is normally provisioned lazily by the email tool's first
        # initialize. A signup that runs before that would register with an
        # EMPTY address, so make sure it exists here (idempotent).
        try:
            await client.provision(resolve_instance_id())
        except Exception as e:
            raise RuntimeError(f"could not provision the agent inbox for X signup: {e}")

        driver, release = await self._open_signup_driver(user_id)
        self._signup_driver = driver
        self._signup_release = release
        mail = MailPoller(client)
        progress = XSessionStore(provider="x_signup")
        display, handle = self._signup_identity()
        disclosure = self._disclosure()

        class _Identity:
            name = display
            dob = "2000-01-01"
            handle = ""
            disclosure = ""
        _Identity.handle = handle
        _Identity.disclosure = disclosure
        return SignupFlow(driver=driver, mail=mail, store=self.session_store,
                          progress=progress, identity=_Identity(), user_id=user_id)

    @staticmethod
    def _signup_identity() -> tuple:
        """(display name, requested @handle) for the agent's X account.

        ``X_SIGNUP_HANDLE`` lets an operator pick the handle (an instance id is
        often already taken on X); the display name follows the instance id.
        The handle is sanitised to X's rules (letters, digits, underscore,
        <=15 chars) so a bad env value degrades to a legal request, never to a
        form error several steps in.
        """
        import os as _os
        import re as _re
        from core.instance import resolve_instance_id
        iid = resolve_instance_id()
        raw = (_os.environ.get("X_SIGNUP_HANDLE") or iid).strip().lstrip("@")
        clean = _re.sub(r"[^A-Za-z0-9_]", "", raw)[:15]
        handle = clean or _re.sub(r"[^A-Za-z0-9_]", "", iid)[:15]
        return iid.capitalize(), handle

    def _disclosure(self) -> str:
        """The automation-disclosure bio. ``X_SIGNUP_DISCLOSURE`` wins when set;
        the default names the instance, never the owner's INTERNAL tenant id
        (``resolve_owner_principal`` returns things like ``local`` or a numeric
        Telegram id — 'operated by local' is not a disclosure)."""
        import os as _os
        from core.instance import resolve_instance_id
        custom = (_os.environ.get("X_SIGNUP_DISCLOSURE") or "").strip()
        if custom:
            return custom[:160]
        return (f"Automated account: {resolve_instance_id()} is an autonomous "
                f"POLYROB agent. Posts are generated by software.")[:160]

    def _agentmail_client(self):
        import os as _os
        from tools.email_providers.agentmail import AgentMailClient
        key = _os.environ.get("AGENTMAIL_API_KEY", "")
        return AgentMailClient(key) if key else None

    async def _open_signup_driver(self, user_id: str):
        """Fresh (no storage_state) X-scoped context for signup."""
        if self.container is None:
            raise RuntimeError("no container — browser unavailable")
        bm = self.container.get_service("browser_manager")
        if bm is None:
            raise RuntimeError("browser_manager not available")
        if not getattr(bm, "is_initialized", False):
            await bm.initialize()
        browser = await bm.get_browser()
        from tools.browser.context import BrowserContextConfig
        cfg = BrowserContextConfig(allowed_domains=X_ALLOWED_DOMAINS)
        ctx = await browser.new_context(config=cfg, session_id=f"xsignup_{user_id}")
        session = await ctx.get_session()
        from tools.x_browser.driver import XPageDriver
        driver = XPageDriver(session.current_page)

        async def _release():
            try:
                await ctx.close()
            except Exception as e:
                logger.debug("x signup context close failed: %s", e)
        return driver, _release

    async def _escalate(self, paused, user_id, session_id, driver=None):
        from tools.x_browser.escalation import escalate_and_wait
        # Raw attribute — never trip the lazy Container singleton when unset.
        container = getattr(self, "_container", None)
        return await escalate_and_wait(
            container, user_id, session_id, paused,
            timeout_sec=600, driver=driver)

    @BaseTool.action(
        "Check whether the agent's saved X session is still logged in.",
        param_model=XLoginCheckAction,
    )
    async def x_login_check(self, params: XLoginCheckAction = None,
                            execution_context=None) -> ActionResult:
        user_id = self._user_id(execution_context)
        if not self.session_store.exists(user_id):
            return ActionResult(
                extracted_content="no X session stored — run `polyrob x-account capture-session`.",
                include_in_memory=True)
        try:
            driver, release = await self._open_driver(user_id)
        except Exception as e:
            return ActionResult(error=f"could not open X session: {e}",
                                include_in_memory=True)
        try:
            ok = await driver.is_logged_in()
        finally:
            await self._run_release(release)
        if ok:
            return ActionResult(extracted_content="X session is logged in.",
                                include_in_memory=True)
        return ActionResult(
            extracted_content="X session is expired — run `polyrob x-account capture-session` "
                              "to re-capture the login.",
            include_in_memory=True)
