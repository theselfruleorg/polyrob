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
import re
from email.header import decode_header
from email.message import Message
from typing import Any, Optional

from surfaces.email.dedup import MessageDedup
from surfaces.email.fetchers import MailFetchError
from surfaces.email.inbound import dedup_key, process_email
from surfaces.email.surface import EmailSurface

logger = logging.getLogger(__name__)

#: How many times one message may raise in routing before the loop gives up on
#: it and marks it handled. Bounded so a poison message cannot block the
#: mailbox forever, and loud so it is never a silent drop (D4).
_MAX_ROUTE_ATTEMPTS = 3


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parts = []
        for chunk, enc in decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts)
    except Exception:
        return value


#: What the turn text says when an email carried no body we could read. Named,
#: never blank: an empty turn is indistinguishable from a message that said
#: nothing, and the agent answers it as though the sender wrote nothing.
NO_BODY_NOTE = "[this email had no readable text body]"

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    """A text rendering of an HTML body: tags removed, entities unescaped.

    Not a renderer — a legibility floor. D27: an HTML-only email (every
    marketing client, and Outlook by default) produced an EMPTY turn, so the
    agent replied to nothing.
    """
    import html as _html

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _plain_body(em: Message) -> str:
    """The message's readable text: ``text/plain``, else ``text/html`` stripped,
    else :data:`NO_BODY_NOTE` — never a silent empty string (D27)."""
    try:
        html = ""
        if em.is_multipart():
            for part in em.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    text = _decode_part(part)
                    if text.strip():
                        return text
                elif ctype == "text/html" and not html:
                    html = _decode_part(part)
        else:
            text = _decode_part(em) or (em.get_payload() or "")
            if em.get_content_type() == "text/html":
                html, text = text, ""
            if text.strip():
                return text
        if html.strip():
            stripped = strip_html(html)
            if stripped:
                return stripped
        return NO_BODY_NOTE
    except Exception as e:
        logger.warning("email body extraction failed: %s", e, exc_info=True)
        return NO_BODY_NOTE


def _attachments(em: Message) -> list:
    """Every attached part as ``{filename, mime, data}`` (2026-09-13 media rail).

    ``_plain_body`` above returns at the FIRST ``text/plain`` part and everything
    else was discarded — an owner emailing a PDF got an answer written as if the
    mail were empty. Only parts with an explicit attachment disposition or a
    filename are taken, so the body and its ``text/html`` twin never show up here.
    """
    out: list = []
    try:
        if not em.is_multipart():
            return out
        for part in em.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
            if disposition != "attachment" and not filename:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception as e:
                # D64: a part whose transfer-encoding we cannot decode is still
                # an attachment the sender sent. Keeping it with ``data=None``
                # makes the rail NAME it and say it could not be read; dropping
                # it made the agent answer as if it were never attached.
                logger.warning("email attachment %r could not be decoded: %s",
                               filename, e)
                payload = None
            out.append({
                "filename": _decode(filename) if filename else None,
                "mime": part.get_content_type(),
                "data": payload or None,
            })
    except Exception as e:  # a malformed MIME tree must never lose the whole mail
        logger.debug("email attachment extraction failed: %s", e)
    return out


def normalize_email_message(em: Message) -> dict:
    """Map a parsed email to the normalized dict ``process_email`` consumes. Pure."""
    return {
        "message_id": (em.get("Message-ID") or "").strip(),
        "from": _decode(em.get("From")),
        "subject": _decode(em.get("Subject")),
        "body": _plain_body(em),
        "in_reply_to": (em.get("In-Reply-To") or "").strip(),
        "references": (em.get("References") or "").strip(),
        "attachments": _attachments(em),
    }


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
        #: dedup key -> consecutive routing failures (D4's poison guard).
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

        ⚠️ D4 (2026-09-21 interface audit): a message is marked handled ONLY
        after it has been dispatched. Before this the mark ran in a ``finally``
        and the Message-ID dedup key was recorded BEFORE routing, so a crash,
        a restart, or any raising route left the mail both ``\\Seen`` AND
        deduped — permanently invisible, with a swallowed debug line as its
        only trace. The fetch itself no longer sets ``\\Seen`` either
        (``BODY.PEEK[]``), so an undispatched message stays UNSEEN and the next
        poll picks it up.

        The re-processing loop that ordering protected against is closed by the
        poison guard instead: a message that raises repeatedly is marked after
        ``_MAX_ROUTE_ATTEMPTS`` tries and reported, never silently retried
        forever.
        """
        from surfaces.telegram.harness import act_on_inbound  # shared decision executor
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
        for handle, norm in messages:
            key = dedup_key(norm)
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
            except Exception as e:
                self._attempts[key] = self._attempts.get(key, 0) + 1
                if self._attempts[key] < _MAX_ROUTE_ATTEMPTS:
                    logger.error(
                        "email message routing failed (attempt %d/%d, will retry "
                        "next poll): %s", self._attempts[key], _MAX_ROUTE_ATTEMPTS,
                        e, exc_info=True)
                    continue
                logger.error(
                    "email message %s failed %d times — marking it handled so the "
                    "poll loop can proceed; it will NOT be routed: %s",
                    key, self._attempts[key], e, exc_info=True)
            # Dispatched (or given up on): record the dedup key, THEN mark the
            # transport handle. Both are idempotent.
            self._attempts.pop(key, None)
            self._record_handled(key)
            self.fetcher.mark_handled(handle)
        return routed

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
