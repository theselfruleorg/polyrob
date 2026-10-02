"""FeishuSurface — Feishu / Lark as a Surface contract impl.

send() resolves the chat from the session_key chat segment and posts plain text
through the Open-API client, which splits at the platform limit. Buffered
streaming (ABC default); no edit. Order 0004: ``OutboundMessage.media`` entries
are uploaded (image → im/v1/images, anything else → im/v1/files) and sent after
the text; a failed upload is NAMED in one short follow-up line — the text has
already landed. Order 0005: explicit ``OutboundMessage.actions`` render as ONE
interactive card of buttons after the text (``cards.py``); the text keeps the
same tappable tokens, so a card that fails to send costs nothing. Fail-open.
"""
from __future__ import annotations

import logging
from typing import Any

from core.surfaces.envelopes import (OutboundMessage, SendResult,
                                     SurfaceCapabilities)
from core.surfaces.surface import Surface
from surfaces.feishu.client import MAX_TEXT_CHARS

logger = logging.getLogger(__name__)


def chat_id_from_session_key(session_key: str) -> str:
    from core.surfaces.session_chat_registry import chat_id_from_session_key as _p
    return _p(session_key)


class FeishuSurface(Surface):
    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    @property
    def surface_id(self) -> str:
        return "feishu"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,   # buffered flush via the ABC default
            supports_edit=False,
            is_multi_tenant=True,
            max_message_bytes=MAX_TEXT_CHARS,
            markdown_flavor="none",    # a text message renders no markdown
            media_out=True,            # order 0004: image / file upload
            supports_actions=True,     # order 0005: explicit actions → a card
        )

    async def send(self, msg: OutboundMessage) -> SendResult:
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        chat = chat_id_from_session_key(msg.session_key)
        try:
            sent = await self._client.send_message(chat, msg.text or "")
            mid = (sent or {}).get("message_id")
        except Exception as e:  # fail-open
            logger.error("FeishuSurface.send to %s failed: %s", chat, e)
            return SendResult(success=False, error=str(e))
        if msg.media:
            await self._send_media(chat, msg.media)
        if msg.actions:
            await self._send_actions(chat, msg.actions)
        return SendResult(success=True, surface_message_id=str(mid) if mid else None)

    async def _send_actions(self, chat: str, actions: list) -> None:
        from surfaces.feishu.cards import render_card
        card = render_card(actions)           # explicit actions only, re-checked
        if card is None:
            return
        try:
            await self._client.send_card(chat, card)
        except Exception as e:                # the text already carries the commands
            logger.warning("FeishuSurface: card for %s failed: %s", chat, e)

    async def _send_media(self, chat: str, media: list) -> None:
        """Upload each renderable entry after the text. A failure is named to the
        chat (one line per file), never silent and never fatal to the text."""
        import os
        failed = []
        for entry in media:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue                      # not renderable (e.g. an email subject)
            path = str(entry["path"])
            name = os.path.basename(path)
            if not (os.path.isfile(path) and os.access(path, os.R_OK)):
                logger.warning("FeishuSurface: media path missing/unreadable: %s", path)
                failed.append((name, "file not found"))
                continue
            try:
                await self._client.send_media(chat, path, image=entry.get("kind") == "image")
                if entry.get("caption"):
                    await self._client.send_message(chat, str(entry["caption"]))
            except Exception as e:
                logger.warning("FeishuSurface: upload of %s failed: %s", path, e)
                failed.append((name, str(e)[:120]))
        if failed:
            lines = "\n".join(f"Could not attach {n}: {why}" for n, why in failed)
            try:
                await self._client.send_message(chat, lines)
            except Exception as e:
                logger.warning("FeishuSurface: could not name the failed uploads: %s", e)

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        return None
