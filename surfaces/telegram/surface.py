"""P4: TelegramSurface — the Telegram transport as a Surface contract impl.

Runs in the agent/API process over an aiogram Bot (injected, so it's unit-testable
with a fake Bot and no network). send() resolves the target chat id by parsing the
session_key (the chat segment — same convention every surface uses), splits messages
over Telegram's 4096-char limit, and calls bot.send_message.

Streaming: default is the Surface ABC's buffered path (partials buffer, one send() on
finalize). With TELEGRAM_INCREMENTAL_STREAM on (#8), stream() instead opens one message
and live-edits it in place via editMessageText as deltas arrive, flood-throttled
(TELEGRAM_STREAM_EDIT_INTERVAL_SEC) and RetryAfter-aware (the minimal rate limiter).

Formatting: the agent writes markdown; core.surfaces.rendering converts it to the HTML
subset Telegram renders (parse_mode="HTML"). Every send goes through send_text(), which
retries once as plain text if Telegram rejects the markup, so a formatting problem can
never cost the user the message. Fail-open: a bot error returns SendResult(success=False)
/ is logged and swallowed.
"""
import logging
import os
import time as _time
from typing import Any, Optional

from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities
from core.surfaces.rendering import render_for_flavor, split_text

logger = logging.getLogger(__name__)

_TELEGRAM_MAX = 4096
_TELEGRAM_CAPTION_MAX = 1024  # Bot API cap for photo/document captions (< the 4096 message cap)

# 030 L1: bounded flood-control honoring. A 429 carries the server's retry_after;
# before this, the main send path answered it with a plain-text resend (which
# 429s again) and swallowed the loss — a rate-limited owner message was gone.
_FLOOD_RETRIES = 2          # retries per bot call, after the initial attempt
_FLOOD_WAIT_CAP_SEC = 30.0  # never sleep longer than this per wait


def _retry_after_seconds(e: Exception) -> Optional[float]:
    """The server-stated flood delay, or None when *e* is not a RetryAfter.

    Duck-typed on ``.retry_after`` (aiogram's TelegramRetryAfter) so this module
    keeps its no-hard-aiogram-import rule and stays fake-testable.
    """
    ra = getattr(e, "retry_after", None)
    try:
        return float(ra) if ra is not None else None
    except (TypeError, ValueError):
        return None


def chat_id_from_session_key(session_key: str) -> str:
    """Delegates to the ONE inverse parser next to build_session_key
    (030 WS-B3/E1 — this 6-line parse was copied into every surface)."""
    from core.surfaces.session_chat_registry import chat_id_from_session_key as _p
    return _p(session_key)


def thread_id_from_session_key(session_key: str) -> Optional[str]:
    """044 T10: the forum-topic thread id embedded in a room session key
    (``...:thread:<id>``), or None for a plain (non-topic) chat."""
    parts = (session_key or "").split(":")
    if "thread" in parts:
        i = parts.index("thread")
        return parts[i + 1] if i + 1 < len(parts) else None
    return None


class TelegramSurface(Surface):
    def __init__(self, bot: Any) -> None:
        super().__init__()
        self._bot = bot
        self.bot_username: Optional[str] = None

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
            markdown_flavor="html",        # agent markdown -> Telegram HTML (see core.surfaces.rendering)
            media_out=True,                # can render OutboundMessage.media as photo/document
        )

    def _parse_mode(self) -> str | None:
        return "HTML" if self.capabilities.markdown_flavor == "html" else None

    async def _call_flood_controlled(self, chat_id: str, op: str, fn):
        """Run one bot call, honoring RetryAfter with a bounded wait+retry (030 L1).

        Non-flood exceptions propagate unchanged (the caller's markup-downgrade
        logic handles those). Flood penalties are recorded on the rate limiter
        for observability; recording failures never block the retry.
        """
        import asyncio
        attempt = 0
        while True:
            try:
                return await fn()
            except Exception as e:
                delay = _retry_after_seconds(e)
                if delay is None or attempt >= _FLOOD_RETRIES:
                    raise
                attempt += 1
                try:
                    from surfaces.telegram.rate_limit import get_telegram_rate_limiter
                    await get_telegram_rate_limiter().record_penalty(
                        int(chat_id), delay, op)
                except Exception:
                    pass
                wait = min(delay, _FLOOD_WAIT_CAP_SEC)
                logger.warning(
                    "TelegramSurface: flood control on %s (%s) — waiting %.1fs "
                    "(retry %d/%d)", chat_id, op, wait, attempt, _FLOOD_RETRIES)
                await asyncio.sleep(wait)

    def _render_chunks(self, text: str) -> list[str]:
        return self.render_outbound(text or "")

    async def send_text(self, chat_id: str, text: str, *, reply_to: Optional[str] = None,
                        thread_id: Optional[str] = None) -> Optional[Any]:
        """The ONE outbound text seam: split, convert to Telegram HTML, send.

        Retries a chunk as plain text (the original markdown source) if Telegram rejects
        the markup, so a converter edge case degrades formatting instead of dropping the
        message. ``reply_to``/``thread_id`` thread the reply to the room line it answers
        and to the forum topic it belongs to (044 T10); a stale ``reply_to`` (the anchor
        message was deleted/inaccessible) self-heals — the send is retried once without
        it rather than the reply being lost. Returns the last message_id. Raises only if
        every attempt fails.
        """
        limit = self.capabilities.max_message_bytes
        rendered = render_for_flavor(text or "", self.capabilities.markdown_flavor, limit)
        # Same splitter, same args -> same chunk count; if that ever stops holding, the
        # plain-text retry falls back to the rendered chunk rather than dropping a message.
        sources = split_text(text or "", limit)
        if len(sources) != len(rendered):
            sources = rendered
        parse_mode = self._parse_mode()
        extra: dict = {}
        if thread_id:
            try:
                extra["message_thread_id"] = int(thread_id)
            except (TypeError, ValueError):
                logger.debug("TelegramSurface: bad thread_id %r ignored", thread_id)
        if reply_to:
            try:
                extra["reply_to_message_id"] = int(reply_to)
            except (TypeError, ValueError):
                logger.debug("TelegramSurface: bad reply_to %r ignored", reply_to)
        last_id = None
        for body, source in zip(rendered, sources):
            try:
                sent = await self._send_chunk(chat_id, body, parse_mode, extra)
            except Exception as e:
                # A flood error is NOT a markup error — after bounded retries it
                # propagates; a plain-text resend would just 429 again (030 L1).
                if not parse_mode or _retry_after_seconds(e) is not None:
                    raise
                logger.warning("TelegramSurface: %s rejected, resending as plain text: %s", parse_mode, e)
                sent = await self._send_chunk(chat_id, source, None, extra)
            last_id = getattr(sent, "message_id", None)
        return last_id

    async def _send_chunk(self, chat_id: str, body: str, parse_mode, extra: dict):
        """One flood-controlled ``send_message`` call, self-healing a stale reply
        anchor (044 T10): the message this turn answers can be deleted/expired between
        the trigger and our reply, and Telegram rejects the WHOLE send for it — retry
        once without ``reply_to_message_id`` rather than losing the message. Mutates
        ``extra`` in place so later chunks in the same ``send_text`` call don't repeat
        the failed anchor."""
        try:
            return await self._call_flood_controlled(
                chat_id, "send",
                lambda: self._bot.send_message(chat_id, body, parse_mode=parse_mode, **extra))
        except Exception as e:
            if "reply_to_message_id" in extra and "message to be replied not found" in str(e):
                logger.info("TelegramSurface: stale reply anchor for %s, resending unthreaded", chat_id)
                extra.pop("reply_to_message_id", None)
                return await self._call_flood_controlled(
                    chat_id, "send",
                    lambda: self._bot.send_message(chat_id, body, parse_mode=parse_mode, **extra))
            raise

    async def send(self, msg: OutboundMessage) -> SendResult:
        # If this discrete reply finalizes an in-flight streamed bubble, commit it in
        # place (no duplicate message) and we're done. No-op when streaming is off.
        if await self._finalize_live_on_send(msg):
            return SendResult(success=True)
        chat_id = chat_id_from_session_key(msg.session_key)
        try:
            last_id = await self.send_text(
                chat_id, msg.text or "", reply_to=msg.reply_to,
                thread_id=thread_id_from_session_key(msg.session_key))
            # Media is best-effort ON TOP of the text: a media send failure (missing
            # file, bot rejection, ...) never takes the text down with it — the text
            # above has already landed. See _send_media.
            if msg.media:
                failed = await self._send_media(chat_id, msg.media,
                                                self._parse_mode())
                if failed:
                    # ⚠️ D56: SAID, not just logged. The text above carries the
                    # message; the reader needs to know a picture is MISSING
                    # rather than wonder whether they missed it — which is
                    # exactly what an unsent invoice CARD looks like. The
                    # harness's own reply path already says this; the surface's
                    # did not, so every router-delivered card failed silently.
                    await self.send_text(
                        chat_id,
                        f"(I could not attach {failed} file(s) — everything "
                        f"you need is in the message above.)")
            return SendResult(success=True, surface_message_id=str(last_id) if last_id is not None else None)
        except Exception as e:  # fail-open: never raise into the loop
            logger.error("TelegramSurface.send to %s failed: %s", chat_id, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    def _caption_for(self, text: str) -> Optional[str]:
        if not text:
            return None
        chunks = render_for_flavor(text, self.capabilities.markdown_flavor, _TELEGRAM_CAPTION_MAX)
        if len(chunks) == 1:
            return chunks[0]
        # 030 (F5): an over-length explicit caption was silently cut at 1024 —
        # keep the cut, but say so instead of pretending the caption was whole.
        head = render_for_flavor(text, self.capabilities.markdown_flavor,
                                 _TELEGRAM_CAPTION_MAX - 1)[0]
        return head + "…"

    async def _send_media(self, chat_id: str, media: list, parse_mode) -> int:
        """Send each renderable media entry (path + kind) as a photo/document.

        Returns the number of entries that did NOT go (D56) so the caller can
        say so. Fail-open per entry: a missing/unreadable file or a raising bot
        call is logged at WARN and the next entry is tried — the text above is
        never affected (this runs after the text send succeeds).

        030 L8: the caption is the entry's EXPLICIT ``caption`` only. The message
        text has already been sent as its own bubble — repeating its first 1024
        chars under every photo/document doubled every media send (worst on the
        spill path, where the gist reappeared as the report's caption). This also
        aligns with ``TelegramBotSink._send_media``, which never duplicated."""
        # Lazy import — surfaces/telegram avoids a hard aiogram import at module load
        # (mirrors harness.py/voice.py); only paid when there is media to send.
        from aiogram.types import FSInputFile

        failed = 0
        for entry in media:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path:
                continue  # not a renderable entry (e.g. the legacy email-subject shape)
            if not (os.path.isfile(path) and os.access(path, os.R_OK)):
                logger.warning("TelegramSurface: media path missing/unreadable, skipping: %s", path)
                failed += 1
                continue
            caption = self._caption_for(entry.get("caption") or "")
            try:
                file = FSInputFile(path, filename=os.path.basename(path))
                if entry.get("kind") == "image":
                    await self._call_flood_controlled(
                        chat_id, "send_photo",
                        lambda: self._bot.send_photo(
                            chat_id, file, caption=caption, parse_mode=parse_mode))
                else:
                    await self._call_flood_controlled(
                        chat_id, "send_document",
                        lambda: self._bot.send_document(
                            chat_id, file, caption=caption, parse_mode=parse_mode))
            except Exception as e:
                logger.warning("TelegramSurface: failed to send media %s: %s", path, e)
                failed += 1
        return failed

    # --- #8 incremental streaming: the engine lives in the base Surface; Telegram only
    #     supplies the transport primitives (send/edit/overflow) + its policy hooks. ---

    def _incremental_streaming_enabled(self) -> bool:
        from core.surfaces.config import SurfaceConfig
        return SurfaceConfig.telegram_incremental_stream()

    def _stream_target(self, msg: OutboundMessage) -> str:
        return chat_id_from_session_key(msg.session_key)

    def _edit_min_interval_sec(self) -> float:
        from core.surfaces.config import SurfaceConfig
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
