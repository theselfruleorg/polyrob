"""Surface ABC: thin required surface (Hermes parity) + fat shared defaults.

Required (every surface MUST implement): surface_id, capabilities, send(), start(),
stop().

Shared, default-provided (a surface gets these for free):
  - stream(): buffers deltas and flushes one send() on finalize, OR — when the surface
    opts into incremental streaming AND advertises supports_edit — runs the generic
    live-edit engine below (open one message, edit it in place as deltas arrive,
    flood-throttle, split overflow on finalize). The engine is surface-AGNOSTIC; a
    surface plugs in four small transport primitives (_open_stream_message,
    _edit_stream_message, _send_stream_overflow, _stream_target) plus two policy hooks
    (_incremental_streaming_enabled, _edit_min_interval_sec). This is what lets Telegram
    today and WhatsApp/etc. tomorrow share one streaming state machine.
  - identify(): defaults to None.

The principle: the base abstraction holds the maximum shared machinery; each surface
adds only what is genuinely transport-specific on top.

STATUS (incremental streaming): wired end-to-end. feed.py publishes a turn's deltas with a
stable per-TURN stream_id (the session_key); under an edit-capable surface with the flag on,
they open+edit ONE live message. The turn's discrete reply (the curated send_message text)
arrives via send(), which calls _finalize_live_on_send() to COMMIT that same bubble in place
— so the PERSISTED final is the clean reply, never raw stream tokens, and there is no
duplicate message. A bounded _MAX_LIVE_STREAMS cap bounds any un-finalized stream (e.g. a
turn that streams but ends via done() instead of a reply).

CAVEAT (why it stays opt-in / default-OFF): the INTERMEDIATE frames shown mid-stream are the
raw model deltas, scrubbed per-chunk by MessageRouter (scrub_brain_blocks) — best-effort, so
a brain/think block straddling a chunk boundary can briefly surface before the clean final
commit. The persisted final is always clean. Enable TELEGRAM_INCREMENTAL_STREAM only where
that intermediate exposure is acceptable; OFF keeps the buffered one-send-on-finalize path.
"""
import logging
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

from core.surfaces.envelopes import (
    OutboundMessage, SendResult, SurfaceCapabilities, Identity,
)

logger = logging.getLogger(__name__)


@dataclass
class SurfaceConfigField:
    name: str
    secret: bool = False
    required: bool = True
    env_alias: str | None = None
    help: str = ""


@dataclass
class SurfaceConfigSchema:
    fields: list = field(default_factory=list)


def split_message(text: str, limit: int) -> List[str]:
    """Split text into <=limit-sized chunks (shared by send() and the stream engine)."""
    if not text:
        return [text]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


class _LiveStream:
    """In-progress streamed reply for one stream_id: the single message we keep editing
    in place, plus accumulated text and last-render bookkeeping. Surface-agnostic —
    ``target`` is whatever address the concrete surface's _stream_target() returns."""
    __slots__ = ("target", "message_id", "text", "rendered", "last_edit")

    def __init__(self, target: Any) -> None:
        self.target = target
        self.message_id: Optional[Any] = None
        self.text = ""
        self.rendered = ""        # last text actually pushed to the surface
        self.last_edit = 0.0      # monotonic ts of last edit (flood throttle)


class Surface(ABC):
    def __init__(self) -> None:
        self._stream_buffers: dict[str, list] = {}
        self._live: dict[str, _LiveStream] = {}

    @property
    @abstractmethod
    def surface_id(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> SurfaceCapabilities: ...

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> SendResult: ...

    @abstractmethod
    async def start(self, container) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def stream(self, msg: OutboundMessage) -> None:
        """Route a streamed delta: live-edit when the surface opts in + supports edit,
        else the buffered default (commit one send() on finalize)."""
        if self._incremental_streaming_enabled() and self.capabilities.supports_edit:
            return await self._stream_incremental(msg)
        key = msg.stream_id or msg.session_key
        if msg.partial:
            buf = self._stream_buffers.get(key)
            if buf is None:
                # M4: bound the number of buffered streams (evict oldest). The streaming
                # mirror may never emit a partial=False finalize, so without this the
                # buffer map grows for the daemon's lifetime.
                if len(self._stream_buffers) >= self._MAX_LIVE_STREAMS:
                    oldest = next(iter(self._stream_buffers))
                    self._stream_buffers.pop(oldest, None)
                buf = self._stream_buffers[key] = []
            buf.append(msg.text)
            # And bound each un-finalized stream's chunk list (drop oldest chunks) so a
            # single never-flushed key can't grow without limit.
            if len(buf) > self._MAX_STREAM_CHUNKS:
                del buf[:len(buf) - self._MAX_STREAM_CHUNKS]
            return
        buffered = "".join(self._stream_buffers.pop(key, []))
        final_text = buffered + (msg.text or "")
        await self.send(OutboundMessage(
            session_key=msg.session_key, text=final_text, kind=msg.kind,
            partial=False, stream_id=msg.stream_id, reply_to=msg.reply_to,
        ))

    async def identify(self, raw: dict) -> Optional[Identity]:
        return None

    @classmethod
    def config_schema(cls) -> "SurfaceConfigSchema":
        return SurfaceConfigSchema(fields=[])

    @classmethod
    def validate_config(cls, values: dict) -> tuple[bool, str]:
        missing = [f.name for f in cls.config_schema().fields
                   if f.required and not values.get(f.name)]
        return (not missing, "" if not missing else f"missing: {', '.join(missing)}")

    def render_outbound(self, text: str) -> List[str]:
        """Escape + split outbound text per this surface's capabilities. Surfaces should
        use this instead of re-implementing per-platform chunking."""
        from core.surfaces.rendering import render_for_flavor
        caps = self.capabilities
        return render_for_flavor(text, caps.markdown_flavor, caps.max_message_bytes)

    def can_send_now(self, session_key: str, *, now: Optional[float] = None) -> "SendDecision":
        """Whether a (proactive) send to this chat is allowed right now. Base default:
        ALLOW (free outbound). A windowed surface overrides this. Pure + sync so producers
        can consult it cheaply before enqueuing."""
        from core.surfaces.send_policy import SendDecision
        return SendDecision.ALLOW

    async def _finalize_live_on_send(self, msg: OutboundMessage) -> bool:
        """If an in-flight live stream exists for this message's session, COMMIT it with
        this (clean, curated) discrete text instead of letting the surface send a separate
        message — so the turn's discrete reply finalizes the streamed bubble in place (no
        duplicate, and the persisted final text is the clean reply, not raw stream tokens).

        A surface's send() calls this first and returns early when it returns True. No-op
        (returns False) when incremental streaming is off, the surface can't edit, or no
        live stream is open for this session — so the normal send path is unchanged."""
        if not (self._incremental_streaming_enabled() and self.capabilities.supports_edit):
            return False
        key = msg.stream_id or msg.session_key
        st = self._live.pop(key, None)
        if st is None or st.message_id is None:
            return False
        await self._commit_final_stream(st, msg.text or "")
        return True

    # --- incremental streaming engine (generic; surfaces provide the primitives) ---
    #
    # A surface enables this by returning True from _incremental_streaming_enabled()
    # and advertising capabilities.supports_edit, then implementing the transport
    # primitives below. Everything here is shared.

    def _incremental_streaming_enabled(self) -> bool:
        """Opt-in switch (default OFF -> buffered)."""
        return False

    def _stream_target(self, msg: OutboundMessage) -> Any:
        """The surface-specific address to send/edit against (default: the session_key;
        Telegram parses the chat id out of it)."""
        return msg.session_key

    def _edit_min_interval_sec(self) -> float:
        """Minimum seconds between in-place edits (flood-control). 0 = edit every delta."""
        return 0.0

    async def _open_stream_message(self, target: Any, text: str) -> Optional[Any]:
        """Send the first visible delta as a new message; return its id (for editing)."""
        raise NotImplementedError

    async def _edit_stream_message(self, target: Any, message_id: Any, text: str) -> None:
        """Replace the open message's text in place."""
        raise NotImplementedError

    async def _send_stream_overflow(self, target: Any, text: str) -> None:
        """Emit text past the first message's size limit as a fresh message."""
        raise NotImplementedError

    def _on_stream_error(self, target: Any, op: str, e: Exception) -> None:
        """Handle a transport error during streaming (default: log). A surface can
        override to record provider back-off (e.g. Telegram RetryAfter)."""
        logger.debug("surface %s stream %s failed for %s: %s", self.surface_id, op, target, e)

    # Defensive cap on in-flight streams. The engine finalizes (and pops) a stream when a
    # matching partial=False arrives; until the live producers emit such a finalize with a
    # STABLE per-turn stream_id (current feed.py uses a per-STEP stream_id and no finalize
    # reaches stream()), an enabled surface could otherwise accumulate one entry per step.
    # This bound makes the worst case a bounded ring, never an unbounded leak. See the
    # module docstring: incremental streaming is engine-correct but not yet wired live —
    # keep the opt-in flag OFF until the producer emits per-turn finalizes.
    _MAX_LIVE_STREAMS = 256
    _MAX_STREAM_CHUNKS = 4096  # cap buffered deltas per un-finalized stream (M4)

    async def _stream_incremental(self, msg: OutboundMessage) -> None:
        key = msg.stream_id or msg.session_key
        if msg.partial:
            st = self._live.get(key)
            if st is None:
                if len(self._live) >= self._MAX_LIVE_STREAMS:
                    # Evict the oldest in-flight stream (insertion order) so the map stays
                    # bounded even if finalizes never arrive.
                    oldest = next(iter(self._live))
                    self._live.pop(oldest, None)
                st = _LiveStream(self._stream_target(msg))
                self._live[key] = st
            st.text += (msg.text or "")
            await self._render_live(st, final=False)
            return
        # Finalize: commit the full text and drop the live-stream state.
        st = self._live.pop(key, None)
        final_text = (st.text if st else "") + (msg.text or "")
        if st is None or st.message_id is None:
            # No live message was opened (all-whitespace partials, or a bare finalize)
            # -> a normal send so the reply still lands.
            await self.send(OutboundMessage(
                session_key=msg.session_key, text=final_text, kind=msg.kind,
            ))
            return
        await self._commit_final_stream(st, final_text)

    async def _render_live(self, st: _LiveStream, *, final: bool) -> None:
        max_bytes = self.capabilities.max_message_bytes
        text = (st.text or "")[:max_bytes]
        if not text.strip():
            return
        if st.message_id is None:
            try:
                st.message_id = await self._open_stream_message(st.target, text)
                st.rendered = text
                st.last_edit = _time.monotonic()
            except Exception as e:  # fail-open: never raise into the agent loop
                self._on_stream_error(st.target, "open", e)
            return
        if not final:
            if _time.monotonic() - st.last_edit < self._edit_min_interval_sec():
                return
        if text == st.rendered:
            return  # an unchanged edit is rejected by some surfaces ("not modified")
        await self._do_edit(st, text)

    async def _commit_final_stream(self, st: _LiveStream, final_text: str) -> None:
        chunks = split_message(final_text, self.capabilities.max_message_bytes)
        first = chunks[0] if chunks else ""
        if first and first != st.rendered:
            await self._do_edit(st, first)
        for chunk in chunks[1:]:
            try:
                await self._send_stream_overflow(st.target, chunk)
            except Exception as e:
                self._on_stream_error(st.target, "overflow", e)

    async def _do_edit(self, st: _LiveStream, text: str) -> None:
        try:
            await self._edit_stream_message(st.target, st.message_id, text)
            st.rendered = text
            st.last_edit = _time.monotonic()
        except Exception as e:
            self._on_stream_error(st.target, "edit", e)
