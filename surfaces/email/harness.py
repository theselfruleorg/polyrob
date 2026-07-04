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
from email.header import decode_header
from email.message import Message
from typing import Any, Optional

from surfaces.email.dedup import MessageDedup
from surfaces.email.inbound import process_email
from surfaces.email.surface import EmailSurface

logger = logging.getLogger(__name__)


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


def _plain_body(em: Message) -> str:
    try:
        if em.is_multipart():
            for part in em.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        return payload.decode(part.get_content_charset() or "utf-8",
                                              errors="replace")
            return ""
        payload = em.get_payload(decode=True)
        if payload is not None:
            return payload.decode(em.get_content_charset() or "utf-8", errors="replace")
        return em.get_payload() or ""
    except Exception:
        return ""


def normalize_email_message(em: Message) -> dict:
    """Map a parsed email to the normalized dict ``process_email`` consumes. Pure."""
    return {
        "message_id": (em.get("Message-ID") or "").strip(),
        "from": _decode(em.get("From")),
        "subject": _decode(em.get("Subject")),
        "body": _plain_body(em),
        "in_reply_to": (em.get("In-Reply-To") or "").strip(),
        "references": (em.get("References") or "").strip(),
    }


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

        Every fetched message is marked ``\\Seen`` after handling (whether routed,
        deduped, or errored) so it leaves the UNSEEN set — belt-and-suspenders with the
        Message-ID dedup, so a dedup-DB loss can't reopen a reprocess-every-poll loop
        (Fusion HIGH).
        """
        from surfaces.telegram.harness import act_on_inbound  # shared decision executor
        routed = 0
        try:
            messages = await self._fetch_unread()
        except Exception as e:
            logger.debug("email poll fetch failed: %s", e)
            return 0
        for num, em in messages:
            try:
                norm = normalize_email_message(em)
                result = await process_email(
                    self.container, norm, dedup=self.dedup,
                    user_directory=self.user_directory,
                )
                if result is not None:
                    await act_on_inbound(self.task_agent, result)
                    routed += 1
            except Exception as e:
                logger.debug("email message routing failed: %s", e)
            finally:
                self._mark_seen(num)
        return routed

    def _mark_seen(self, num) -> None:
        try:
            conn = getattr(self.email_tool, "imap_connection", None)
            if conn is not None and num is not None:
                conn.store(num, "+FLAGS", "\\Seen")
        except Exception as e:
            logger.debug("email mark-seen %s failed: %s", num, e)

    async def _fetch_unread(self) -> list:
        """Fetch unread messages as (imap_num, parsed email.Message) pairs (IMAP I/O)."""
        import email as _email
        tool = self.email_tool
        await tool.ensure_initialized()
        if not getattr(tool, "imap_connection", None):
            await tool._connect_imap()
        conn = tool.imap_connection
        conn.select("INBOX")
        _, nums = conn.search(None, "UNSEEN")
        out = []
        for num in (nums[0].split() if nums and nums[0] else []):
            try:
                _, data = conn.fetch(num, "(RFC822)")
                out.append((num, _email.message_from_bytes(data[0][1])))
            except Exception as e:
                logger.debug("email fetch %s failed: %s", num, e)
        return out

    async def run_polling(self) -> None:
        await self.start()
        logger.info("📧 email harness polling every %ss", self.poll_interval)
        while not self._stop:
            try:
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
