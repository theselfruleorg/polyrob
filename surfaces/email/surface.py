"""WS-B EmailSurface — the email transport as a Surface contract impl.

Outbound only carries the agent's reply to a bound email session; it goes out over an
injected SMTP sender (the existing ``EmailTool`` or any object with an async
``send_email(to, subject, body)``), so it's unit-testable with a fake and no network.

Streaming: buffered (``supports_edit=False``) — email has no in-place edit, so the
Surface ABC accumulates partials and emits ONE message on finalize (never one email per
delta). ``supports_interactive_ask=False`` — email is asynchronous, no live prompt.
Fail-open: a sender error returns ``SendResult(success=False)`` and is swallowed.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.surface import Surface

logger = logging.getLogger(__name__)

# Generous cap — emails are long-form; the base split_message will still chunk if a
# single body somehow exceeds this (one email per chunk).
_EMAIL_MAX = 1_000_000
_DEFAULT_SUBJECT = "Re: your message"


def address_from_session_key(session_key: str) -> str:
    """Extract the recipient email address from a session_key.

    Chat-scoped keys: ``agent:main:email:dm:{addr}[:user][:thread:..]`` -> the chat
    segment (index 4). Falls back to the last segment for any other shape. The address
    can contain no ':' so index-4 is unambiguous.
    """
    parts = session_key.split(":")
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


class EmailSurface(Surface):
    def __init__(self, sender: Any, *, default_subject: str = _DEFAULT_SUBJECT) -> None:
        super().__init__()
        self._sender = sender
        self._default_subject = default_subject
        self._container: Any = None

    @property
    def surface_id(self) -> str:
        return "email"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,        # buffered: one email per finalized turn
            supports_edit=False,            # no in-place edit on email
            supports_interactive_ask=False, # asynchronous medium
            is_multi_tenant=True,
            max_message_bytes=_EMAIL_MAX,
            markdown_flavor="none",
            media_out=True,                 # can render OutboundMessage.media as attachments
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        # supports_edit is False, so this is a no-op (buffered path), but keep the call
        # for parity with the contract.
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        addr = address_from_session_key(msg.session_key)
        subject = (msg.media[0].get("subject") if msg.media and isinstance(msg.media[0], dict)
                   else None) or self._default_subject
        # Path-bearing entries become attachments; the legacy {"subject": ...}-only
        # entry above stays subject metadata, never an attachment (no "path" key).
        attachments = [m.get("path") for m in (msg.media or [])
                       if isinstance(m, dict) and m.get("path")] or None
        # A5 (2026-07-13 review): seed the reply binding BEFORE the email leaves.
        # A cap-refused binding BLOCKS the send — previously the mail still went out
        # and the reply was orphaned forever (unroutable, invisibly).
        seed_state = self._maybe_seed_correspondent(msg.session_key, addr)
        if seed_state == "refused":
            logger.warning(
                "EmailSurface.send to %s blocked: correspondent per-day cap reached — "
                "no reply binding could be created, so the email was NOT sent", addr)
            return SendResult(
                success=False,
                error=("correspondent per-day cap reached — reply binding refused; "
                       "email not sent (raise CORRESPONDENT_MAX_NEW_PER_DAY or approve "
                       "pending correspondents)"))
        try:
            chat_row = self._chat_row(msg.session_key)
            # A3: prefer the Message-ID-returning sender so the outbound can be
            # bound to a thread anchor (reply In-Reply-To -> exact resolve), and
            # thread OUR reply into the correspondent's mailbox via In-Reply-To
            # (last inbound Message-ID from the conversation store).
            sender_ex = getattr(self._sender, "send_email_ex", None)
            if callable(sender_ex):
                irt = self._last_inbound_mid(chat_row, addr)
                mid = await sender_ex(addr, subject, msg.text or "",
                                      attachments=attachments, in_reply_to=irt)
                ok = bool(mid)
            else:  # legacy sender (tests/fakes): bool contract, no anchor
                mid = None
                ok = await self._sender.send_email(addr, subject, msg.text or "",
                                                   attachments=attachments)
            if ok:
                if mid:
                    self._maybe_seed_thread_anchor(msg.session_key, addr, str(mid))
                self._record_outbound_conversation(chat_row, addr, msg.text or "",
                                                   mid, subject)
            return SendResult(success=bool(ok))
        except Exception as e:  # fail-open: never raise into the loop
            logger.error("EmailSurface.send to %s failed: %s", addr, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    def _maybe_seed_correspondent(self, session_key: str, addr: str) -> Optional[str]:
        """Register the recipient as a (guard-railed) correspondent BEFORE sending, so
        their reply can later route back as DATA. Approval-gated and fail-soft — a seed
        FAULT never affects the send; only an explicit cap "refused" blocks it (the
        caller's decision). Returns the seed state, or None when seeding was skipped
        (no container / no bound session / fault)."""
        container = self._container
        if container is None:
            return None
        try:
            chat_reg = container.get_service("session_chat_registry")
            row = chat_reg.resolve(session_key) if chat_reg else None
            if not row:
                return None
            from core.surfaces.seed import maybe_seed_correspondent
            return maybe_seed_correspondent(
                container, surface="email", address=addr,
                session_id=row.get("session_id"), user_id=row.get("user_id"),
                thread_id=None, provenance="owner",
            )
        except Exception as e:
            logger.debug("EmailSurface correspondent seed skipped: %s", e)
            return None

    def _chat_row(self, session_key: str) -> Any:
        """The bound session row ({'session_id','user_id',...}) or None. Fail-soft."""
        container = self._container
        if container is None:
            return None
        try:
            chat_reg = container.get_service("session_chat_registry")
            return chat_reg.resolve(session_key) if chat_reg else None
        except Exception:
            return None

    def _conversation_store(self):
        try:
            return (self._container.get_service("conversation_store")
                    if self._container else None)
        except Exception:
            return None

    def _last_inbound_mid(self, chat_row: Any, addr: str) -> Any:
        """Last inbound Message-ID for this correspondent (In-Reply-To). Fail-soft."""
        if not chat_row:
            return None
        store = self._conversation_store()
        if store is None:
            return None
        try:
            conv = store.get(chat_row.get("user_id") or "", "email", addr)
            return (conv or {}).get("last_inbound_mid") or None
        except Exception:
            return None

    def _record_outbound_conversation(self, chat_row: Any, addr: str, body: str,
                                      mid: Any, subject: str) -> None:
        """E1: append the outbound to the durable conversation log. Fail-soft."""
        if not chat_row:
            return
        store = self._conversation_store()
        if store is None:
            return
        try:
            store.record_outbound(chat_row.get("user_id") or "", "email", addr, body,
                                  mid=(str(mid) if mid else None), subject=subject,
                                  session_id=chat_row.get("session_id"))
        except Exception as e:
            logger.debug("EmailSurface conversation record skipped: %s", e)

    def _maybe_seed_thread_anchor(self, session_key: str, addr: str, mid: str) -> None:
        """Bind the outbound Message-ID to the sending session (A3) so the reply's
        In-Reply-To exact-matches in the registry. Fully fail-soft."""
        container = self._container
        if container is None:
            return
        try:
            chat_reg = container.get_service("session_chat_registry")
            row = chat_reg.resolve(session_key) if chat_reg else None
            registry = container.get_service("correspondent_registry")
            if not row or registry is None or not hasattr(registry, "seed_thread_anchor"):
                return
            registry.seed_thread_anchor(
                surface="email", address=addr, thread_id=mid,
                session_id=row.get("session_id"), user_id=row.get("user_id"))
        except Exception as e:
            logger.debug("EmailSurface thread-anchor seed skipped: %s", e)

    async def start(self, container) -> None:
        self._container = container

    async def stop(self) -> None:
        return None
