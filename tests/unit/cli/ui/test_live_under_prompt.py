"""P0 regression: Rich Live must NOT run under the REPL's prompt_toolkit patch_stdout.

The REPL wraps its loop in ``prompt_toolkit.patch_stdout()``. A Rich ``Live``
(the ``ActivityLine`` "thinking" spinner and the streaming ``ResponseBox``)
emits cursor-up / erase-line escapes with no trailing newline; prompt_toolkit's
``StdoutProxy`` line-buffers and independently owns the cursor → the transient
erase never lands → stacked ``rob · thinking · 0s`` / ``· 11s`` lines + stray
``\`` artifacts (the live screenshot bug).

Fix: ``RichRenderer(live_allowed=False)`` (set by the REPL) keeps the activity
line dormant and the response box in buffer-only mode, so the pinned
``bottom_toolbar`` is the sole in-flight indicator and the finalized answer is
printed exactly once as a newline-terminated block. ``rob run`` (no
patch_stdout) keeps ``live_allowed=True`` and the live boxes.
"""

from __future__ import annotations

import io
from io import StringIO

import pytest
from rich.console import Console

from cli.ui import select_renderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


class _TTYStream(io.StringIO):
    """A StringIO that claims to be a TTY (forces the Rich path)."""

    def isatty(self):  # noqa: D401
        return True


@pytest.fixture(autouse=True)
def _clean_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    yield


def _tty_console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, width=80, force_terminal=True, highlight=False), buf


def _renderer(console: Console, *, live_allowed: bool) -> RichRenderer:
    return RichRenderer(SessionState(), console=console, live_allowed=live_allowed)


# ---------------------------------------------------------------------------
# Activity line is suppressed under the prompt
# ---------------------------------------------------------------------------


def test_repl_turn_start_does_not_open_activity_live():
    console, _ = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.on_turn_start("do work")
    # No Rich Live region opened (the bottom_toolbar is the indicator instead).
    assert r._activity is None
    r.on_turn_end("done")


def test_one_shot_turn_start_still_opens_activity_live():
    """rob run (no patch_stdout) keeps the live activity line — unchanged."""
    console, _ = _tty_console()
    r = _renderer(console, live_allowed=True)
    r.on_turn_start("do work")
    assert r._activity is not None and r._activity.is_live
    r.on_turn_end("done")


# ---------------------------------------------------------------------------
# Streaming box degrades to buffer-only under the prompt
# ---------------------------------------------------------------------------


def test_repl_stream_delta_stays_buffer_only():
    console, _ = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.on_turn_start("do work")
    r.on_stream_delta("hello ")
    r.on_stream_delta("world")
    # The box accumulates text but never opens a Live (no cursor contention).
    assert r._box is not None
    assert not r._box.is_live
    assert r._box.text == "hello world"


def test_repl_answer_printed_once_at_turn_end():
    console, buf = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.on_turn_start("do work")
    r.on_stream_delta("the answer")
    r.on_turn_end("the answer")
    out = buf.getvalue()
    # The finalized answer is printed exactly once as a static block.
    assert out.count("the answer") == 1


def test_one_shot_stream_delta_opens_live_box():
    console, _ = _tty_console()
    r = _renderer(console, live_allowed=True)
    r.on_turn_start("do work")
    r.on_stream_delta("chunk")
    assert r._box is not None and r._box.is_live
    r.on_turn_end("chunk")


# ---------------------------------------------------------------------------
# In-flight feedback: the toolbar is FROZEN during a turn (prompt_async has
# returned before convo.respond runs), so under the REPL we print one honest
# static "working" line so a silent tool-running stretch isn't dead-silent.
# NOT a Live (no patch_stdout corruption). One-shot + verbose keep their own
# feedback (live activity line / trace).
# ---------------------------------------------------------------------------


def test_repl_turn_start_prints_working_notice():
    console, buf = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.on_turn_start("do work")
    assert "working" in buf.getvalue()


def test_one_shot_turn_start_prints_no_working_notice():
    """rob run uses the live activity line — no static working notice."""
    console, buf = _tty_console()
    r = _renderer(console, live_allowed=True)
    r.on_turn_start("do work")
    assert "working" not in buf.getvalue()
    r.on_turn_end("done")


def test_verbose_repl_turn_start_prints_no_working_notice():
    """Verbose trace IS the feedback — no redundant working notice."""
    console, buf = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.verbose = True
    r.on_turn_start("do work")
    assert "working" not in buf.getvalue()


def test_persistent_app_turn_start_prints_no_working_notice():
    """The persistent box has a live status-bar spinner — the static working
    notice is redundant and must be suppressed (live_status_bar=True)."""
    console, buf = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.live_status_bar = True
    r.on_turn_start("do work")
    assert "working" not in buf.getvalue()


def test_working_notice_does_not_double_count_answer():
    """The working notice must not collide with the once-printed answer."""
    console, buf = _tty_console()
    r = _renderer(console, live_allowed=False)
    r.on_turn_start("do work")
    r.on_stream_delta("the answer")
    r.on_turn_end("the answer")
    assert buf.getvalue().count("the answer") == 1


# ---------------------------------------------------------------------------
# select_renderer wiring
# ---------------------------------------------------------------------------


def test_select_renderer_threads_live_allowed_false():
    r = select_renderer(SessionState(), stream=_TTYStream(), live_allowed=False)
    assert isinstance(r, RichRenderer)
    assert r._live_allowed is False


def test_select_renderer_defaults_live_allowed_true():
    r = select_renderer(SessionState(), stream=_TTYStream())
    assert isinstance(r, RichRenderer)
    assert r._live_allowed is True
