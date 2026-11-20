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
from core.surfaces.surface import Surface

logger = logging.getLogger(__name__)

_SLACK_MAX = 4000


def channel_id_from_session_key(session_key: str) -> str:
    """Delegates to the ONE inverse parser next to build_session_key
    (030 WS-B3/E1 — this 6-line parse was copied into every surface)."""
    from core.surfaces.session_chat_registry import chat_id_from_session_key as _p
    return _p(session_key)


def thread_ts_from_session_key(session_key: str):
    """OS7: the ``thread_ts`` of a ``...:thread:<ts>`` key (the segment
    ``build_session_key`` appends), or None for a key without a thread."""
    _, sep, tail = (session_key or "").rpartition(":thread:")
    return tail or None if sep else None


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
            markdown_flavor="mrkdwn",  # agent markdown -> Slack mrkdwn (core.surfaces.rendering)
            media_out=True,            # 030 D6: external-upload flow
            supports_actions=True,     # order 0017: explicit actions → Block Kit buttons (blocks.py)
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        channel = channel_id_from_session_key(msg.session_key)
        thread_ts = thread_ts_from_session_key(msg.session_key)
        last_ts = None
        try:
            from surfaces.slack.blocks import SECTION_MAX, render_blocks
            has_actions = bool(render_blocks(" ", msg.actions))   # explicit actions only
            # A section holds 3000 chars: with buttons the text splits to that.
            # The agent's markdown becomes Slack mrkdwn (core.surfaces.rendering).
            from core.surfaces.rendering import render_for_flavor
            chunks = render_for_flavor(msg.text or "", self.capabilities.markdown_flavor,
                                       SECTION_MAX if has_actions else _SLACK_MAX)
            for i, chunk in enumerate(chunks):
                # The buttons ride the LAST chunk; the text keeps the commands.
                extra = ({"blocks": render_blocks(chunk, msg.actions)}
                         if has_actions and i == len(chunks) - 1 else {})
                sent = await self._client.send_message(channel, chunk,
                                                       thread_ts=thread_ts, **extra)
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
