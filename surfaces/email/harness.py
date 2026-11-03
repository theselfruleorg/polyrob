"""WS-B email harness — IMAP poll loop over the transport-free inbound spine.

The pure ``normalize_email_message`` (an ``email.message.Message`` -> the normalized
dict ``process_email`` expects) is unit-tested without a mailbox; the IMAP poll loop is
a thin, fail-open shell around it (network I/O, verified live like the Telegram surface).

v1 is correspondent-only: owner-by-email is OFF, so an email sender is at most a
CORRESPONDENT (their reply -> DATA into the originating session) or DENIED.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from surfaces.email.dedup import MessageDedup
from surfaces.email.fetchers import MailFetchError
from surfaces.email.inbound import dedup_key, process_email
from surfaces.email.surface import EmailSurface
# The parser lives in the tool tier so the agent's mailbox verbs share it; the
# names stay importable from here (tests and fetchers import them from harness).
from tools.email_providers.mime import (  # noqa: F401
    NO_BODY_NOTE, _attachments, _decode, _decode_part, _plain_body,
    normalize_email_message, strip_html,
)

logger = logging.getLogger(__name__)

#: How many times one message may raise in routing before the loop gives up on
#: it and marks it handled. Bounded so a poison message cannot block the
#: mailbox forever, and loud so it is never a silent drop (D4).
_MAX_ROUTE_ATTEMPTS = 3


async def _media_bytes(media):
    """``fetch_bytes`` for the shared inbound-media rail: email attachments arrive
    complete, so there is nothing to download."""
    return getattr(media, "data", None)


class EmailHarness:
    """Poll an IMAP mailbox; route each new message through the inbound spine.

    ``email_tool`` supplies SMTP send (for the EmailSurface) + IMAP connection config;
    the loop fetches RFC822, normalizes, dedups, routes, and acts. Fail-open throughout.
    """

    def __init__(self, container: Any, task_agent: Any, *, email_tool: Any,
                 data_dir: str = "data", poll_interval: float = 60.0) -> None:
        self.container = container
        self.task_agent = task_agent
        self.email_tool = email_tool
        self.poll_interval = poll_interval
        self.dedup = MessageDedup(os.path.join(data_dir, "email_dedup.db"))
        # TS-20: the per-message state lives in the same DB as the dedup rows.
        try:
            from surfaces.email.inbound_state import InboundState
            self.inbound_state: Optional[Any] = InboundState(self.dedup.db_path)
        except Exception:
            logger.warning("email inbound state DB unavailable — in-memory counter",
                           exc_info=True)
            self.inbound_state = None
        # Transport seam (Task 5, 2026-08-18): the fetcher owns "get new mail" +
        # the durable handled-mark; poll_once stays transport-blind.
        from surfaces.email.fetchers import AgentMailFetcher, ImapFetcher
        if getattr(email_tool, "provider", "smtp") == "agentmail":
            self.fetcher = AgentMailFetcher(email_tool.agentmail, self.dedup)
        else:
            self.fetcher = ImapFetcher(email_tool)
        # A UserDirectory is REQUIRED to identify inbound senders. Only the telegram
        # harness ever constructed/registered one, so `polyrob email` had none and
        # every inbound email crashed identification and was silently dropped after
        # being marked \Seen. Mirror surfaces/telegram/harness.py: build + register
        # one on the shared data dir when the container has none.
        ud = container.get_service("user_directory") if container else None
        if ud is None:
            from tools.user_directory import UserDirectory
            ud = UserDirectory(os.path.join(data_dir, "users.db"))
            if container is not None:
                try:
                    container.register_service("user_directory", ud)
                except Exception as e:
                    logger.debug("email: user_directory registration failed: %s", e)
        self.user_directory = ud
        self.surface = EmailSurface(email_tool)
        self._stop = False
        #: fallback counter when the state DB is unavailable (D4's poison guard).
        self._attempts: dict = {}

    async def start(self) -> None:
        from core.surfaces.registry import register_surface
        try:
            register_surface(self.container, self.surface)
        except Exception as e:
            logger.debug("email surface registration failed: %s", e)

    async def stop(self) -> None:
        self._stop = True

    async def poll_once(self) -> int:
        """Fetch unread messages and route them. Returns the count routed.

        ⚠️ D4: a message is marked handled ONLY after it has been dispatched;
        the fetch does not set ``\\Seen`` (``BODY.PEEK[]``), so an undispatched
        message stays UNSEEN and the next poll picks it up.

        ⚠️ TS-20: each message has ONE durable row (``inbound_state``) and every
        move is a guarded write: ``fetched -> routed -> handled``, or
        ``fetched -> failed(n)``, and at ``_MAX_ROUTE_ATTEMPTS`` failures
        ``quarantined`` — marked ``\\Seen`` to stop the loop AND reported to the
        owner once. The counter survives a restart (it was process memory), and
        a message whose FETCH itself raises is counted too (it was skipped with
        a warning every poll, for ever). A row already ``routed`` is never
        routed again: the turn ran; only the mark was lost.
        """
        from surfaces._actor import act_on_inbound  # shared decision executor
        routed = 0
        try:
            messages = await self.fetcher.fetch_unread()
        except MailFetchError as e:
            self._note_fetch_outage(e)
            return 0
        except Exception as e:
            logger.error("email poll fetch failed: %s", e, exc_info=True)
            return 0
        self._clear_fetch_outage()
        for failure in list(getattr(self.fetcher, "last_fetch_failures", None) or []):
            await self._count_fetch_failure(failure)
        for handle, norm in messages:
            key = dedup_key(norm)
            row = self._state_begin(key, norm)
            state = row.get("state") if row else "fetched"
            if state in ("routed", "handled", "quarantined"):
                # Dispatched (or given up on) earlier; only the mark was lost.
                if state == "routed":
                    self._state_move(key, "routed", "handled")
                await self._mark(handle)
                continue
            try:
                result = await process_email(
                    self.container, norm, dedup=self.dedup,
                    user_directory=self.user_directory,
                    record_dedup=False,
                )
                if result is not None:
                    # Bytes are already on the Media (IMAP hands us the whole
                    # message), so the fetcher is a straight read. The shared rail
                    # only absorbs on an OWNER-tier turn; a CORRESPONDENT's
                    # attachment is named in the text and never written to a
                    # workspace (a From: header is forgeable).
                    await act_on_inbound(self.task_agent, result,
                                         fetch_media=_media_bytes)
                    routed += 1
                    self._state_move(key, "fetched", "routed")
            except Exception as e:
                new = self._state_fail(key, e)
                if new != "quarantined":
                    logger.error(
                        "email message %s routing failed (%s; max %d, will retry "
                        "next poll): %s", key, new or "state unchanged",
                        _MAX_ROUTE_ATTEMPTS, e, exc_info=True)
                    continue
                logger.error(
                    "email message %s failed %d times — quarantined (marked \\Seen, "
                    "owner told); it will NOT be routed: %s",
                    key, _MAX_ROUTE_ATTEMPTS, e, exc_info=True)
                await self._notify_quarantine(norm, key, e)
                self._record_handled(key)
                await self._mark(handle)
                continue
            # Dispatched: record the dedup key, mark the transport handle, then
            # close the row. Each step is idempotent.
            self._record_handled(key)
            await self._mark(handle)
            self._state_move(key, "routed", "handled")
            self._state_move(key, "fetched", "handled")  # nothing was routed
        return routed

    async def _mark(self, handle: Any) -> None:
        mark_async = getattr(self.fetcher, "mark_handled_async", None)
        if callable(mark_async):
            await mark_async(handle)   # EM1: IMAP stays off the loop
        else:
            self.fetcher.mark_handled(handle)

    # -- TS-20 per-message state (fail-open: a broken state DB never stops mail)

    def _state_begin(self, key: str, norm: dict) -> Optional[dict]:
        if self.inbound_state is None:
            return None
        try:
            return self.inbound_state.begin(key, sender=str(norm.get("from") or ""),
                                            subject=str(norm.get("subject") or ""))
        except Exception:
            logger.warning("email inbound state begin failed for %s", key, exc_info=True)
            return None

    def _state_move(self, key: str, frm: str, to: str) -> bool:
        if self.inbound_state is None:
            return False
        try:
            row = self.inbound_state.get(key)
            if not row or row["state"] != frm:
                return False
            return self.inbound_state.move(key, frm, to)
        except Exception:
            logger.warning("email inbound state %s -> %s failed for %s", frm, to, key,
                           exc_info=True)
            return False

    def _state_fail(self, key: str, exc: BaseException) -> Optional[str]:
        """Count one failure; ``"quarantined"`` once the cap is reached. Without
        a state DB the in-memory counter of D4 stands in."""
        if self.inbound_state is not None:
            try:
                return self.inbound_state.fail(key, f"{type(exc).__name__}: {exc}",
                                               max_attempts=_MAX_ROUTE_ATTEMPTS)
            except Exception:
                logger.warning("email inbound state fail() failed for %s", key, exc_info=True)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        return "quarantined" if self._attempts[key] >= _MAX_ROUTE_ATTEMPTS else "failed"

    async def _count_fetch_failure(self, failure: Any) -> None:
        """A message whose FETCH/parse raised: counted under its UID key, and
        quarantined (marked + owner told) at the cap."""
        handle = getattr(failure, "handle", None)
        key_fn = getattr(handle, "key", None)
        if not callable(key_fn):
            return
        key = key_fn()
        self._state_begin(key, {"subject": "(unreadable message)"})
        new = self._state_fail(key, RuntimeError(getattr(failure, "error", "fetch failed")))
        if new == "quarantined":
            await self._notify_quarantine({"from": "", "subject": ""}, key,
                                          getattr(failure, "error", "fetch failed"))
            await self._mark(handle)

    async def _notify_quarantine(self, norm: dict, key: str, err: Any) -> None:
        """ONE owner notice per quarantined message (the row moved once)."""
        sender = str(norm.get("from") or "") or "an unknown sender"
        subject = str(norm.get("subject") or "") or "(no subject)"
        text = (f"An email could not be read or routed after {_MAX_ROUTE_ATTEMPTS} tries "
                f"and was set aside: from {sender}, subject \"{subject[:120]}\". "
                f"It is marked read in the inbox; open it there to act on it. "
                f"Last error: {str(err)[:200]}")
        try:
            from core.event_log import emit
            emit("email_quarantined", source="surfaces.email",
                 attrs={"key": key[:200], "from": sender[:200], "subject": subject[:200],
                        "error": str(err)[:300]})
        except Exception:
            pass
        try:
            from core.instance import resolve_owner_user_id
            owner = resolve_owner_user_id()
            if owner:
                import core.surfaces.user_delivery as _ud
                await _ud.deliver_user_message(self.container, owner, text,
                                               source="email_quarantined")
        except Exception:
            logger.warning("email quarantine owner notice failed (%s)", key, exc_info=True)

    def _record_handled(self, key: str) -> None:
        """Record the dedup key for a message that HAS been dispatched."""
        try:
            self.dedup.seen(key)
        except Exception as e:
            logger.warning("email dedup record failed for %s: %s", key, e,
                           exc_info=True)

    def _note_fetch_outage(self, exc: "MailFetchError") -> None:
        """D28: an inbound outage is a FACT with a remedy, not silence.

        An auth failure gets a durable verdict (``core.credential_verdicts``),
        so every status seat renders it with a SINCE clock beside the SMTP row
        instead of the mailbox simply going quiet.
        """
        if getattr(exc, "auth", False):
            try:
                from core.credential_verdicts import record_rejection, warn_once
                v = record_rejection(
                    "imap", exc.key or "",
                    code="auth",
                    remedy="re-check the mailbox password / API key, then restart "
                           "the email surface")
                if warn_once("imap", exc.key or "", episode=v.first_seen):
                    logger.error("email INBOUND is down: %s", exc)
            except Exception:
                logger.error("email INBOUND is down: %s", exc, exc_info=True)
            return
        logger.error("email inbound fetch failed (no new mail cannot be "
                     "distinguished from this): %s", exc)

    def _clear_fetch_outage(self) -> None:
        """A successful read is what clears the standing inbound verdict."""
        try:
            from core.credential_verdicts import clear_rejection, verdict
            key = getattr(self.fetcher, "_verdict_key", None)
            key = key() if callable(key) else ""
            if verdict("imap", key or "") is not None:
                clear_rejection("imap", key or "")
                logger.info("email inbound recovered — IMAP verdict cleared")
        except Exception:
            logger.debug("imap verdict clear skipped", exc_info=True)

    def _note_smtp_verdict(self) -> None:
        """Say ONCE per outage that the SEND half is down — never probe it.

        057 WS-F: the poll loop needs IMAP only, so a rejected SMTP login no
        longer costs a handshake and an ERROR line every 60 s (1,439/day on
        prod). The operator still learns about it, and a NEW verdict episode
        (recorded when the TTL lapses and a real send re-probes and fails)
        speaks again. Fail-open: a tool without the seam says nothing.
        """
        tool = self.email_tool
        refusal_fn = getattr(tool, "_smtp_verdict_refusal", None)
        if refusal_fn is None:
            return
        try:
            refusal = refusal_fn()
            if refusal:
                tool._warn_smtp_verdict_once(refusal)
        except Exception as e:
            logger.debug("smtp verdict check failed: %s", e)

    async def run_polling(self) -> None:
        await self.start()
        logger.info("📧 email harness polling every %ss", self.poll_interval)
        while not self._stop:
            try:
                self._note_smtp_verdict()
                n = await self.poll_once()
                if n:
                    logger.info("email harness routed %d message(s)", n)
            except Exception as e:
                logger.debug("email poll loop error: %s", e)
            await asyncio.sleep(self.poll_interval)


def build_email_harness(container: Any, task_agent: Any, *, email_tool: Any,
                        data_dir: str = "data", poll_interval: float = 60.0) -> EmailHarness:
    return EmailHarness(container, task_agent, email_tool=email_tool,
                        data_dir=data_dir, poll_interval=poll_interval)
