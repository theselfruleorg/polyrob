"""DingTalkSurface — DingTalk as a Surface contract impl (text only, order 0007).

send() resolves the conversation from the session_key chat segment and hands the
text to the client, which uses the conversation's session webhook while it lives
and the robot OpenAPI after it (so the reply window's ``outside`` is ``allow``:
the window says which PATH carries the reply, not whether one may be sent).
Buffered streaming (ABC default); no edit, no media, no actions yet. Fail-open.
"""
from __future__ import annotations

import logging
from typing import Any

from core.surfaces.envelopes import (OutboundMessage, ReplyWindow, SendResult,
                                     SurfaceCapabilities)
from core.surfaces.surface import Surface
from surfaces.dingtalk.client import MAX_TEXT_CHARS

logger = logging.getLogger(__name__)

#: The nominal session-webhook lifetime; each event carries its own expiry,
#: which the client's conversation cache honours.
SESSION_WEBHOOK_TTL_S = 90 * 60


def chat_id_from_session_key(session_key: str) -> str:
    from core.surfaces.session_chat_registry import chat_id_from_session_key as _p
    return _p(session_key)


class DingTalkSurface(Surface):
    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    @property
    def surface_id(self) -> str:
        return "dingtalk"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,   # buffered flush via the ABC default
            supports_edit=False,
            is_multi_tenant=True,
            max_message_bytes=MAX_TEXT_CHARS,
            markdown_flavor="none",    # a text message renders no markdown
            reply_window=ReplyWindow("session_webhook", SESSION_WEBHOOK_TTL_S,
                                     outside="allow"),
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        chat = chat_id_from_session_key(msg.session_key)
        try:
            sent = await self._client.send_text(chat, msg.text or "")
            mid = (sent or {}).get("processQueryKey")
            return SendResult(success=True, surface_message_id=str(mid) if mid else None)
        except Exception as e:  # fail-open
            logger.error("DingTalkSurface.send to %s failed: %s", chat, e)
            return SendResult(success=False, error=str(e))

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
