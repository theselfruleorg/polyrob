"""Tests for cli.ui.activity.ActivityLine + its RichRenderer lifecycle."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

from cli.ui.activity import ActivityLine
from cli.ui.events import normalize
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


def _non_tty_console() -> Console:
    return Console(file=StringIO(), width=80, no_color=True, highlight=False)


def _tty_console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, width=80, force_terminal=True, highlight=False), buf


# ---------------------------------------------------------------------------
# ActivityLine unit behaviour
# ---------------------------------------------------------------------------


def test_compose_text_thinking_initially():
    line = ActivityLine(_non_tty_console(), clock=lambda: 0.0)
    assert "rob" in line.compose_text()
    assert "thinking" in line.compose_text()


def test_compose_text_counts_and_elapsed():
    now = {"t": 0.0}
    line = ActivityLine(_non_tty_console(), clock=lambda: now["t"])
    line.note_step(1, tool_actions=2)
    line.note_step(2, tool_actions=1)
    now["t"] = 12.4
    text = line.compose_text()
    assert "working" in text
    assert "3 tools" in text
    assert "step 2" in text
    assert "12s" in text


def test_non_terminal_console_stays_dormant():
    console = _non_tty_console()
    line = ActivityLine(console)
    line.start()
    assert not line.is_live
    line.stop()  # idempotent, no output
    assert console.file.getvalue() == ""


def test_terminal_console_starts_and_stops_live():
    console, buf = _tty_console()
    line = ActivityLine(console)
    line.start()
    assert line.is_live
    line.stop()
    assert not line.is_live
    line.stop()  # idempotent


def test_start_after_stop_is_a_noop():
    console, _ = _tty_console()
    line = ActivityLine(console)
    line.start()
    line.stop()
    line.start()
    assert not line.is_live


def test_activity_line_uses_reduced_refresh_rate():
    """10Hz is unnecessary for a once-a-second elapsed-time line and costs a
    needless repaint-thread wakeup rate during every active turn."""
    console, _ = _tty_console()
    line = ActivityLine(console, clock=lambda: 0.0)
    with patch("rich.live.Live") as mock_live_cls:
        mock_live_cls.return_value = MagicMock()
        line.start()
    _, kwargs = mock_live_cls.call_args
    assert kwargs["refresh_per_second"] <= 5


# ---------------------------------------------------------------------------
# RichRenderer lifecycle integration
# ---------------------------------------------------------------------------


_TOOL_STEP = {
    "type": "step",
    "step": 1,
    "data": {
        "actions": [
            {"action_type": "read_file", "name": "read_file", "service": "fs",
             "params": {"file_path": "x"}},
            {"action_type": "send_message", "name": "message", "service": "send",
             "params": {"text": "hi"}},
        ],
        "agent_name": "executor",
        "reasoning": "",
        "context": {"outputs": {"memory": ""}},
    },
}


def _renderer(console: Console) -> tuple[RichRenderer, SessionState]:
    state = SessionState()
    return RichRenderer(state, console=console), state


def test_turn_start_opens_activity_line_on_tty():
    console, _ = _tty_console()
    r, _ = _renderer(console)
    r.on_turn_start("do work")
    assert r._activity is not None and r._activity.is_live
    r.on_turn_end("done")
    assert r._activity is None


def test_verbose_skips_activity_line():
    console, _ = _tty_console()
    r, _ = _renderer(console)
    r.verbose = True
    r.on_turn_start("do work")
    assert r._activity is None


def test_step_event_feeds_activity_counters():
    console, _ = _tty_console()
    r, state = _renderer(console)
    r.on_turn_start("do work")
    activity = r._activity
    ev = normalize(_TOOL_STEP)
    state.update(ev)
    r.on_event(ev)
    text = activity.compose_text()
    # One step, one non-message tool action (send_message excluded).
    assert "step 1" in text
    assert "1 tool" in text
    r.on_turn_end("x")


def test_stream_delta_stops_activity_before_box_opens():
    """One Live per console: the box can only start if the line stopped."""
    console, _ = _tty_console()
    r, _ = _renderer(console)
    r.on_turn_start("do work")
    assert r._activity is not None and r._activity.is_live
    r.on_stream_delta("chunk")
    assert r._activity is None
    # The box took over the Live slot successfully (TTY path).
    assert r._box is not None and r._box.is_live
    r.on_turn_end("chunk")


def test_new_turn_replaces_previous_activity():
    console, _ = _tty_console()
    r, _ = _renderer(console)
    r.on_turn_start("one")
    first = r._activity
    r.on_turn_end("a")
    r.on_turn_start("two")
    assert r._activity is not first
    assert r._activity.is_live
    r.on_turn_end("b")
