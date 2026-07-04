"""The incremental-streaming engine lives in the BASE Surface, so any surface gets it
by implementing four transport primitives + two policy hooks. This exercises the engine
through a minimal NON-Telegram surface — proving the machinery is shared, not bespoke."""
import pytest

from core.surfaces.surface import Surface
from core.surfaces.envelopes import OutboundMessage, SendResult, SurfaceCapabilities


class _MiniSurface(Surface):
    """Smallest possible streaming surface: just the primitives. max 10 chars/message."""
    def __init__(self, enabled=True, supports_edit=True):
        super().__init__()
        self._enabled = enabled
        self._supports_edit = supports_edit
        self.opened = []      # (target, text)
        self.edits = []       # (target, message_id, text)
        self.overflow = []    # (target, text)
        self.sent = []        # text (buffered/fallback send)

    @property
    def surface_id(self):
        return "mini"

    @property
    def capabilities(self):
        return SurfaceCapabilities(
            supports_streaming=True, supports_edit=self._supports_edit, max_message_bytes=10,
        )

    async def send(self, msg):
        self.sent.append(msg.text)
        return SendResult(success=True)

    async def start(self, container):
        return None

    async def stop(self):
        return None

    # opt-in + primitives
    def _incremental_streaming_enabled(self):
        return self._enabled

    def _stream_target(self, msg):
        return "T"

    def _edit_min_interval_sec(self):
        return 0.0

    async def _open_stream_message(self, target, text):
        self.opened.append((target, text))
        return 1

    async def _edit_stream_message(self, target, message_id, text):
        self.edits.append((target, message_id, text))

    async def _send_stream_overflow(self, target, text):
        self.overflow.append((target, text))


def _om(text, partial, sid="s"):
    return OutboundMessage(session_key="k", text=text, partial=partial, stream_id=sid)


@pytest.mark.asyncio
async def test_base_engine_streams_for_any_surface():
    s = _MiniSurface()
    await s.stream(_om("ab", partial=True))
    await s.stream(_om("cd", partial=True))
    await s.stream(_om("ef", partial=False))
    assert s.opened == [("T", "ab")]          # one open
    assert s.edits[-1] == ("T", 1, "abcdef")   # accumulated final edit
    assert s.sent == []                        # never fell back to send


@pytest.mark.asyncio
async def test_base_engine_splits_overflow_on_finalize():
    s = _MiniSurface()  # max 10 chars
    await s.stream(_om("12345", partial=True))               # opens
    await s.stream(_om("67890ABCDE", partial=False))          # total 15 chars
    assert s.edits[-1] == ("T", 1, "1234567890")             # first 10 edited in place
    assert s.overflow == [("T", "ABCDE")]                    # remainder as overflow


@pytest.mark.asyncio
async def test_disabled_falls_back_to_buffered_send():
    s = _MiniSurface(enabled=False)
    await s.stream(_om("ab", partial=True))
    await s.stream(_om("cd", partial=False))
    assert s.opened == [] and s.edits == []
    assert s.sent == ["abcd"]   # base buffered path


@pytest.mark.asyncio
async def test_live_map_is_bounded_when_finalize_never_arrives(monkeypatch):
    """Fusion H1 guard: with per-step stream_ids that never finalize, _live must stay
    bounded (ring-evict oldest) instead of leaking one entry per step."""
    s = _MiniSurface()
    monkeypatch.setattr(type(s), "_MAX_LIVE_STREAMS", 8, raising=False)
    for i in range(50):
        await s.stream(_om("x", partial=True, sid=f"turn:{i}"))  # unique id, no finalize
    assert len(s._live) <= 8


@pytest.mark.asyncio
async def test_no_edit_support_falls_back_to_buffered():
    """A surface that opts in but can't edit must NOT use the live engine."""
    s = _MiniSurface(enabled=True, supports_edit=False)
    await s.stream(_om("ab", partial=True))
    await s.stream(_om("cd", partial=False))
    assert s.opened == [] and s.edits == []
    assert s.sent == ["abcd"]
