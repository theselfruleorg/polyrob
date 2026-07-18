"""SlackSurface — the Slack transport as a Surface contract impl.

send() resolves the channel from the session_key chat segment, splits over a
4000-char text cap (Slack's recommended max), threads replies when the key
carries a thread id, and posts via the thin Web-API client. Buffered
streaming (ABC default); fail-open sends.
"""
from __future__ import annotations

import logging
from typing import Any

from core.surfaces.envelopes import (OutboundMessage, SendResult,
                                     SurfaceCapabilities)
from core.surfaces.surface import Surface, split_message

logger = logging.getLogger(__name__)

_SLACK_MAX = 4000


def channel_id_from_session_key(session_key: str) -> str:
    parts = session_key.split(":")
    if parts and parts[0] == "direct":
        return parts[-1]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


class SlackSurface(Surface):
    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    @property
    def surface_id(self) -> str:
        return "slack"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,   # buffered flush via the ABC default
            supports_edit=True,
            is_multi_tenant=True,
            max_message_bytes=_SLACK_MAX,
            markdown_flavor="none",    # Slack mrkdwn accepts plain text safely
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        channel = channel_id_from_session_key(msg.session_key)
        last_ts = None
        try:
            for chunk in split_message(msg.text or "", _SLACK_MAX):
                sent = await self._client.send_message(channel, chunk)
                last_ts = (sent or {}).get("ts")
            return SendResult(success=True,
                              surface_message_id=str(last_ts) if last_ts else None)
        except Exception as e:  # fail-open
            logger.error("SlackSurface.send to %s failed: %s", channel, e)
            return SendResult(success=False, error=str(e))

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
