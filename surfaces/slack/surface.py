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
    """Delegates to the ONE inverse parser next to build_session_key
    (030 WS-B3/E1 — this 6-line parse was copied into every surface)."""
    from core.surfaces.session_chat_registry import chat_id_from_session_key as _p
    return _p(session_key)


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
            media_out=True,            # 030 D6: external-upload flow
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
            if msg.media:
                await self._send_media(channel, msg.media)
            return SendResult(success=True,
                              surface_message_id=str(last_ts) if last_ts else None)
        except Exception as e:  # fail-open
            logger.error("SlackSurface.send to %s failed: %s", channel, e)
            return SendResult(success=False, error=str(e))

    async def _send_media(self, channel: str, media: list) -> None:
        """Upload each renderable entry after the text (030 D6). Fail-open per
        entry — an upload failure never takes the delivered text down."""
        import os
        for entry in media:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path:
                continue
            if not (os.path.isfile(path) and os.access(path, os.R_OK)):
                logger.warning("SlackSurface: media path missing/unreadable: %s", path)
                continue
            try:
                await self._client.upload_file(
                    channel, path, title=entry.get("caption") or None)
            except Exception as e:
                logger.warning("SlackSurface: failed to upload %s: %s", path, e)

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
