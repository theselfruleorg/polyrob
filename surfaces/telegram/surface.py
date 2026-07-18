"""P4: TelegramSurface — the Telegram transport as a Surface contract impl.

Runs in the agent/API process over an aiogram Bot (injected, so it's unit-testable
with a fake Bot and no network). send() resolves the target chat id by parsing the
session_key (the chat segment — same convention every surface uses), splits messages
over Telegram's 4096-char limit, and calls bot.send_message.

Streaming: default is the Surface ABC's buffered path (partials buffer, one send() on
finalize). With TELEGRAM_INCREMENTAL_STREAM on (#8), stream() instead opens one message
and live-edits it in place via editMessageText as deltas arrive, flood-throttled
(TELEGRAM_STREAM_EDIT_INTERVAL_SEC) and RetryAfter-aware (the minimal rate limiter).

MarkdownV2 is enabled: Messages are escaped via escape_markdown_v2 and sent with
parse_mode="MarkdownV2" for rich formatting. Fail-open: a bot error returns
SendResult(success=False) / is logged and swallowed.
"""
import logging
import os
import time as _time
from typing import Any, Optional

from core.surfaces.surface import Surface, split_message
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from surfaces.telegram.markdown import escape_markdown_v2

logger = logging.getLogger(__name__)

_TELEGRAM_MAX = 4096
_TELEGRAM_CAPTION_MAX = 1024  # Bot API cap for photo/document captions (< the 4096 message cap)


def _markdown_v2_chunks(text: str, limit: int) -> list[str]:
    """Escape and split text so every chunk fits Telegram's message limit."""
    if not text:
        return [""]
    chunks: list[str] = []
    current = ""
    for char in text:
        escaped = escape_markdown_v2(char, allow_skip=False)
        if current and len(current) + len(escaped) > limit:
            chunks.append(current)
            current = escaped
        else:
            current += escaped
    chunks.append(current)
    return chunks


def chat_id_from_session_key(session_key: str) -> str:
    """Extract the Telegram chat id from a session_key.

    Chat-scoped keys: ``agent:main:telegram:{type}:{chat}[:user][:thread:..]`` -> the
    chat segment (index 4). The cron back-compat shim uses ``direct:telegram:{chat}``
    -> the last segment. Falls back to the last segment for any other shape.
    """
    parts = session_key.split(":")
    if parts and parts[0] == "direct":
        return parts[-1]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


class TelegramSurface(Surface):
    def __init__(self, bot: Any) -> None:
        super().__init__()
        self._bot = bot

    @property
    def surface_id(self) -> str:
        return "telegram"

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            supports_streaming=True,      # buffered flush, or live editMessageText (#8)
            supports_edit=True,
            supports_interactive_ask=True,
            is_multi_tenant=True,
            max_message_bytes=_TELEGRAM_MAX,
            markdown_flavor="markdownv2",  # MarkdownV2 with proper escaping via escape_markdown_v2
            media_out=True,                # can render OutboundMessage.media as photo/document
        )

    def _parse_mode(self) -> str | None:
        return "MarkdownV2" if self.capabilities.markdown_flavor == "markdownv2" else None

    def _render_chunks(self, text: str) -> list[str]:
        if self._parse_mode() == "MarkdownV2":
            return _markdown_v2_chunks(text or "", self.capabilities.max_message_bytes)
        return split_message(text or "", self.capabilities.max_message_bytes)

    async def send(self, msg: OutboundMessage) -> SendResult:
        # If this discrete reply finalizes an in-flight streamed bubble, commit it in
        # place (no duplicate message) and we're done. No-op when streaming is off.
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        chat_id = chat_id_from_session_key(msg.session_key)
        last_id = None
        parse_mode = self._parse_mode()
        try:
            for chunk in self._render_chunks(msg.text or ""):
                sent = await self._bot.send_message(chat_id, chunk, parse_mode=parse_mode)
                last_id = getattr(sent, "message_id", None)
            # Media is best-effort ON TOP of the text: a media send failure (missing
            # file, bot rejection, ...) never takes the text down with it — the text
            # above has already landed. See _send_media.
            if msg.media:
                await self._send_media(chat_id, msg.media, parse_mode, msg.text or "")
            return SendResult(success=True, surface_message_id=str(last_id) if last_id is not None else None)
        except Exception as e:  # fail-open: never raise into the loop
            logger.error("TelegramSurface.send to %s failed: %s", chat_id, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    def _caption_for(self, text: str) -> Optional[str]:
        if not text:
            return None
        if self._parse_mode() == "MarkdownV2":
            return _markdown_v2_chunks(text, _TELEGRAM_CAPTION_MAX)[0]
        return split_message(text, _TELEGRAM_CAPTION_MAX)[0]

    async def _send_media(self, chat_id: str, media: list, parse_mode, fallback_text: str) -> None:
        """Send each renderable media entry (path + kind) as a photo/document, alongside
        the text already delivered by send(). Fail-open per entry: a missing/unreadable
        file or a raising bot call is logged at WARN and the next entry is tried — the
        text above is never affected (this runs after the text send succeeds)."""
        # Lazy import — surfaces/telegram avoids a hard aiogram import at module load
        # (mirrors harness.py/voice.py); only paid when there is media to send.
        from aiogram.types import FSInputFile

        for entry in media:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path:
                continue  # not a renderable entry (e.g. the legacy email-subject shape)
            if not (os.path.isfile(path) and os.access(path, os.R_OK)):
                logger.warning("TelegramSurface: media path missing/unreadable, skipping: %s", path)
                continue
            caption = self._caption_for(entry.get("caption") or fallback_text)
            try:
                file = FSInputFile(path, filename=os.path.basename(path))
                if entry.get("kind") == "image":
                    await self._bot.send_photo(chat_id, file, caption=caption, parse_mode=parse_mode)
                else:
                    await self._bot.send_document(chat_id, file, caption=caption, parse_mode=parse_mode)
            except Exception as e:
                logger.warning("TelegramSurface: failed to send media %s: %s", path, e)

    # --- #8 incremental streaming: the engine lives in the base Surface; Telegram only
    #     supplies the transport primitives (send/edit/overflow) + its policy hooks. ---

    def _incremental_streaming_enabled(self) -> bool:
        from agents.task.surface_config import SurfaceConfig
        return SurfaceConfig.telegram_incremental_stream()

    def _stream_target(self, msg: OutboundMessage) -> str:
        return chat_id_from_session_key(msg.session_key)

    def _edit_min_interval_sec(self) -> float:
        from agents.task.surface_config import SurfaceConfig
        return SurfaceConfig.telegram_stream_edit_interval_sec()

    async def _open_stream_message(self, target: str, text: str):
        sent = await self._bot.send_message(target, self._render_chunks(text)[0], parse_mode=self._parse_mode())
        return getattr(sent, "message_id", None)

    async def _edit_stream_message(self, target: str, message_id, text: str) -> None:
        await self._bot.edit_message_text(
            text=text,
            chat_id=target,
            message_id=message_id,
            parse_mode=self._parse_mode(),
        )

    async def _send_stream_overflow(self, target: str, text: str) -> None:
        for chunk in self._render_chunks(text):
            await self._bot.send_message(target, chunk, parse_mode=self._parse_mode())

    async def _commit_final_stream(self, st, final_text: str) -> None:
        chunks = self._render_chunks(final_text)
        first = chunks[0] if chunks else ""
        if first and first != st.rendered:
            await self._do_edit(st, first)
        for chunk in chunks[1:]:
            try:
                await self._bot.send_message(st.target, chunk, parse_mode=self._parse_mode())
            except Exception as e:
                self._on_stream_error(st.target, "overflow", e)

    async def _render_live(self, st, *, final: bool) -> None:
        text = self._render_chunks(st.text or "")[0]
        if not text.strip():
            return
        if st.message_id is None:
            try:
                sent = await self._bot.send_message(st.target, text, parse_mode=self._parse_mode())
                st.message_id = getattr(sent, "message_id", None)
                st.rendered = text
                st.last_edit = _time.monotonic()
            except Exception as e:
                self._on_stream_error(st.target, "open", e)
            return
        if not final and _time.monotonic() - st.last_edit < self._edit_min_interval_sec():
            return
        if text == st.rendered:
            return
        await self._do_edit(st, text)

    def _on_stream_error(self, target: str, op: str, e: Exception) -> None:
        # Record a Telegram RetryAfter penalty so the minimal rate limiter backs off;
        # everything else is just logged (a streamed edit failing is non-fatal).
        retry_after = getattr(e, "retry_after", None)
        if retry_after is not None:
            try:
                import asyncio
                from surfaces.telegram.rate_limit import get_telegram_rate_limiter
                asyncio.ensure_future(
                    get_telegram_rate_limiter().record_penalty(int(target), float(retry_after), op)
                )
            except Exception:
                pass
        logger.debug("TelegramSurface stream %s failed for %s: %s", op, target, e)

    async def start(self, container) -> None:
        return None

    async def stop(self) -> None:
        # The aiogram Bot session is owned/closed by the lifespan, not the surface.
        return None
