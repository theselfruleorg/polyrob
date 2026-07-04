"""Unit tests for cli/ui/streaming.py — the live response box + callback bridge.

Covers (proposal §7.3 / §12 Phase 3 tests):
- 1-chunk and N-chunk sequences produce identical final content.
- finalize() returns the full text and is idempotent (persistable static block).
- Non-TTY degrade path: no console → buffer-only, text still recovered.
- Thread-safety smoke: append() called from a worker thread.
- make_stream_callback filters sub-agent chunks and is fail-open.
"""

from __future__ import annotations

import asyncio
import threading
from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

from cli.ui.streaming import ResponseBox, make_stream_callback


def _tty_console() -> Console:
    """A Rich console that reports as a terminal so Live will start."""
    return Console(file=StringIO(), width=80, no_color=True, force_terminal=True)


# ---------------------------------------------------------------------------
# 1-or-N chunk equivalence
# ---------------------------------------------------------------------------


def test_single_chunk_and_multi_chunk_produce_identical_text():
    """A single full-answer delta and many token deltas → identical content."""
    answer = "The auth flow lives in core/config.py; read it first."

    one = ResponseBox(console=None)
    one.append(answer)
    assert one.finalize() == answer

    many = ResponseBox(console=None)
    for ch in answer:  # one delta per character
        many.append(ch)
    assert many.finalize() == answer

    # Word-sized chunks land on the same content too.
    words = ResponseBox(console=None)
    for tok in answer.split(" "):
        words.append(tok + " ")
    assert words.finalize().strip() == answer.strip()


def test_text_property_reflects_accumulation():
    box = ResponseBox(console=None)
    box.append("ab")
    box.append("cd")
    assert box.text == "abcd"


# ---------------------------------------------------------------------------
# finalize semantics
# ---------------------------------------------------------------------------


def test_finalize_is_idempotent():
    box = ResponseBox(console=None)
    box.append("hello")
    assert box.finalize() == "hello"
    # Second finalize returns the same text, doesn't crash.
    assert box.finalize() == "hello"


def test_streaming_renderer_uses_reduced_refresh_rate():
    """Same rationale as ActivityLine: 10Hz repaint is unnecessary for
    streamed text and costs a needless wakeup rate during active turns."""
    console = _tty_console()
    box = ResponseBox(console=console)
    with patch("rich.live.Live") as mock_live_cls:
        mock_live_cls.return_value = MagicMock()
        box._ensure_live()
    _, kwargs = mock_live_cls.call_args
    assert kwargs["refresh_per_second"] <= 5


def test_finalize_with_live_stops_cleanly():
    """With a TTY console the box starts a Live and finalize stops it."""
    box = ResponseBox(console=_tty_console())
    box.append("streamed answer")
    assert box.is_live  # Live started on first delta
    assert box.finalize() == "streamed answer"
    assert not box.is_live  # Live stopped on finalize


def test_received_chunk_flag():
    box = ResponseBox(console=None)
    assert not box.received_chunk
    box.append("x")
    assert box.received_chunk


def test_empty_delta_is_ignored():
    box = ResponseBox(console=None)
    box.append("")
    assert not box.received_chunk
    assert box.text == ""


# ---------------------------------------------------------------------------
# Non-TTY degrade path
# ---------------------------------------------------------------------------


def test_non_tty_degrades_to_buffer_only():
    """No console → no Live; text is still accumulated and returned."""
    box = ResponseBox(console=None)
    box.append("part1 ")
    box.append("part2")
    assert not box.is_live
    assert box.finalize() == "part1 part2"


def test_late_chunk_after_finalize_keeps_text_coherent():
    box = ResponseBox(console=None)
    box.append("a")
    box.finalize()
    box.append("b")  # late chunk must not crash; text stays coherent
    assert box.text == "ab"


# ---------------------------------------------------------------------------
# Thread-safety smoke
# ---------------------------------------------------------------------------


def test_append_from_worker_thread():
    """append() funnels through a lock — safe from the agent-loop thread."""
    box = ResponseBox(console=None)
    done = threading.Event()

    def worker():
        for i in range(200):
            box.append(str(i % 10))
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    done.wait(timeout=5)
    t.join(timeout=5)
    assert len(box.finalize()) == 200


def test_concurrent_appends_from_multiple_threads():
    box = ResponseBox(console=None)
    threads = [
        threading.Thread(target=lambda: [box.append("x") for _ in range(100)])
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert box.finalize() == "x" * 400


# ---------------------------------------------------------------------------
# make_stream_callback
# ---------------------------------------------------------------------------


class _RecordingRenderer:
    def __init__(self) -> None:
        self.deltas: list[str] = []

    def on_stream_delta(self, delta: str) -> None:
        self.deltas.append(delta)


def test_callback_routes_chunk_to_renderer():
    r = _RecordingRenderer()
    cb = make_stream_callback(r)
    asyncio.run(cb("sid", "executor_1", "hello", 0))
    assert r.deltas == ["hello"]


def test_callback_filters_sub_agent_chunks():
    r = _RecordingRenderer()
    cb = make_stream_callback(r, main_agent_id=lambda: "executor_main")
    asyncio.run(cb("sid", "executor_main", "keep", 0))
    asyncio.run(cb("sid", "researcher_sub", "drop", 0))
    assert r.deltas == ["keep"]


def test_callback_accepts_all_when_main_id_unknown():
    """When the main id accessor returns '', accept every chunk (single-agent)."""
    r = _RecordingRenderer()
    cb = make_stream_callback(r, main_agent_id=lambda: "")
    asyncio.run(cb("sid", "anything", "a", 0))
    asyncio.run(cb("sid", "other", "b", 0))
    assert r.deltas == ["a", "b"]


def test_callback_is_fail_open():
    """A renderer that raises must not propagate into the agent loop."""

    class _Boom:
        def on_stream_delta(self, delta: str) -> None:
            raise RuntimeError("kaboom")

    cb = make_stream_callback(_Boom())
    # Should not raise.
    asyncio.run(cb("sid", "executor", "x", 0))
