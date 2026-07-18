"""DiscordSurface — the Discord transport as a Surface contract impl.

send() resolves the target channel from the session_key chat segment (Discord
DMs and guild channels both address by channel id), splits over the 2000-char
limit, and posts via the thin REST client. Streaming uses the ABC's buffered
default (partials buffer, one send on finalize). Fail-open sends.
"""
from __future__ import annotations

import logging
from typing import Any

from core.surfaces.envelopes import (OutboundMessage, SendResult,
                                     SurfaceCapabilities)
from core.surfaces.surface import Surface, split_message

logger = logging.getLogger(__name__)

_DISCORD_MAX = 2000


def channel_id_from_session_key(session_key: str) -> str:
    """Chat-scoped keys: ``agent:main:discord:{type}:{chat}[...]`` → chat
    segment (index 4); ``direct:discord:{chat}`` → last; else last segment."""
    parts = session_key.split(":")
    if parts and parts[0] == "direct":
        return parts[-1]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


class DiscordSurface(Surface):
    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    @property
    def surface_id(self) -> str:
        return "discord"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,   # buffered flush via the ABC default
            supports_edit=True,
            is_multi_tenant=True,
            max_message_bytes=_DISCORD_MAX,
            markdown_flavor="none",    # Discord renders common markdown as-is
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        channel_id = channel_id_from_session_key(msg.session_key)
        last_id = None
        try:
            for chunk in split_message(msg.text or "", _DISCORD_MAX):
                sent = await self._client.send_message(
                    channel_id, chunk, reply_to=msg.reply_to)
                last_id = (sent or {}).get("id")
            return SendResult(success=True,
                              surface_message_id=str(last_id) if last_id else None)
        except Exception as e:  # fail-open: never raise into the loop
            logger.error("DiscordSurface.send to %s failed: %s", channel_id, e)
            return SendResult(success=False, error=str(e))

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
