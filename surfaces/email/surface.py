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
from typing import Any

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
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        # supports_edit is False, so this is a no-op (buffered path), but keep the call
        # for parity with the contract.
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        addr = address_from_session_key(msg.session_key)
        subject = (msg.media[0].get("subject") if msg.media and isinstance(msg.media[0], dict)
                   else None) or self._default_subject
        try:
            ok = await self._sender.send_email(addr, subject, msg.text or "")
            if ok:
                self._maybe_seed_correspondent(msg.session_key, addr)
            return SendResult(success=bool(ok))
        except Exception as e:  # fail-open: never raise into the loop
            logger.error("EmailSurface.send to %s failed: %s", addr, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    def _maybe_seed_correspondent(self, session_key: str, addr: str) -> None:
        """After an outbound send, register the recipient as a (guard-railed)
        correspondent so their reply can later route back as DATA. Approval-gated and
        fully fail-soft — a seed fault must never affect the send result."""
        container = self._container
        if container is None:
            return
        try:
            chat_reg = container.get_service("session_chat_registry")
            row = chat_reg.resolve(session_key) if chat_reg else None
            if not row:
                return
            from surfaces.email.seed import maybe_seed_correspondent
            maybe_seed_correspondent(
                container, surface="email", address=addr,
                session_id=row.get("session_id"), user_id=row.get("user_id"),
                thread_id=None, provenance="owner",
            )
        except Exception as e:
            logger.debug("EmailSurface correspondent seed skipped: %s", e)

    async def start(self, container) -> None:
        self._container = container

    async def stop(self) -> None:
        return None
