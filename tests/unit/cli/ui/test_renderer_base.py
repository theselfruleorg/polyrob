"""Tests for the Renderer base-class bubble-dedup state machine.

The R2 backstop (byte-identical bubble suppression) and the shared state
``_message_bubble_rendered`` / ``_last_bubble_text`` used to live duplicated in
both PlainRenderer and RichRenderer.  They now live in the ``Renderer`` base
(Task 3.5 refactor) so a single implementation drives both renderers.

These tests assert the shared contract independently of the concrete renderer,
then verify that the same guard fires correctly for BOTH PlainRenderer and
RichRenderer via their ``_handle_step`` leaf methods.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from cli.ui.events import Step
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.renderer import Renderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg_step(text: str, step_n: int = 1) -> Step:
    return Step(
        step=step_n,
        reasoning="",
        memory="",
        actions=[
            {
                "action_type": "send_message",
                "name": "message",
                "service": "send",
                "params": {"text": text, "wait_for_response": False},
            }
        ],
    )


def _plain(one_shot: bool = False) -> tuple[PlainRenderer, io.StringIO]:
    buf = io.StringIO()
    state = SessionState()
    r = PlainRenderer(state=state, stream=buf, one_shot=one_shot)
    return r, buf


def _rich(one_shot: bool = False) -> tuple[RichRenderer, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, width=80, no_color=True, highlight=False)
    state = SessionState()
    r = RichRenderer(state=state, console=console, one_shot=one_shot)
    return r, buf


# ---------------------------------------------------------------------------
# Base-class helpers (_is_bubble_repeat / _mark_bubble_rendered)
# ---------------------------------------------------------------------------


def test_show_tools_defaults_on_verbose_defaults_off():
    """The tool transcript is ON by default (separate axis from /verbose)."""
    for r in (_plain()[0], _rich()[0]):
        assert r.show_tools is True
        assert r.verbose is False


def test_is_bubble_repeat_false_before_any_bubble():
    r, _ = _plain()
    assert r._is_bubble_repeat("hello") is False


def test_is_bubble_repeat_false_after_different_bubble():
    r, _ = _plain()
    r._mark_bubble_rendered("hello")
    assert r._is_bubble_repeat("world") is False


def test_is_bubble_repeat_true_after_same_bubble():
    r, _ = _plain()
    r._mark_bubble_rendered("hello there")
    assert r._is_bubble_repeat("hello there") is True


def test_is_bubble_repeat_ignores_leading_trailing_whitespace():
    """The check is on stripped text: '  hi  ' and 'hi' are the same bubble."""
    r, _ = _plain()
    r._mark_bubble_rendered("  hi  ")
    assert r._is_bubble_repeat("hi") is True
    assert r._is_bubble_repeat("  hi  ") is True


def test_mark_bubble_rendered_sets_state():
    r, _ = _plain()
    assert not r._message_bubble_rendered
    r._mark_bubble_rendered("hey")
    assert r._message_bubble_rendered
    assert r._last_bubble_text == "hey"


# ---------------------------------------------------------------------------
# on_turn_start resets bubble-dedup state (via base)
# ---------------------------------------------------------------------------


def test_on_turn_start_resets_bubble_state_plain():
    r, _ = _plain()
    r._mark_bubble_rendered("previous turn bubble")
    assert r._message_bubble_rendered
    r.on_turn_start("new turn")
    assert not r._message_bubble_rendered
    assert r._last_bubble_text == ""


def test_on_turn_start_resets_bubble_state_rich():
    r, _ = _rich()
    r._mark_bubble_rendered("previous turn bubble")
    assert r._message_bubble_rendered
    r.on_turn_start("new turn")
    assert not r._message_bubble_rendered
    assert r._last_bubble_text == ""


# ---------------------------------------------------------------------------
# R2 backstop: byte-identical repeat suppression via base — PlainRenderer
# ---------------------------------------------------------------------------


def test_plain_suppresses_identical_bubble_repeat():
    """PlainRenderer: a byte-identical second bubble must not print again."""
    r, buf = _plain()
    r.on_turn_start("hi")

    ev1 = _msg_step("Hello, how can I help?", step_n=1)
    r.on_event(ev1)
    after_first = buf.getvalue()
    assert "Hello, how can I help?" in after_first

    ev2 = _msg_step("Hello, how can I help?", step_n=2)
    r.on_event(ev2)
    after_second = buf.getvalue()
    # Text must still appear exactly once — the second is suppressed.
    assert after_second.count("Hello, how can I help?") == 1


def test_plain_does_not_suppress_distinct_bubble():
    """PlainRenderer: a DIFFERENT second bubble must still render."""
    r, buf = _plain()
    r.on_turn_start("hi")

    r.on_event(_msg_step("First message.", step_n=1))
    r.on_event(_msg_step("Second message.", step_n=2))
    out = buf.getvalue()
    assert "First message." in out
    assert "Second message." in out


# ---------------------------------------------------------------------------
# R2 backstop: byte-identical repeat suppression via base — RichRenderer
# ---------------------------------------------------------------------------


def test_rich_suppresses_identical_bubble_repeat():
    """RichRenderer: a byte-identical second bubble must not print again."""
    r, buf = _rich()
    r.on_turn_start("hi")

    ev1 = _msg_step("Hello, how can I help?", step_n=1)
    r.on_event(ev1)
    after_first = buf.getvalue()
    assert "Hello, how can I help?" in after_first

    ev2 = _msg_step("Hello, how can I help?", step_n=2)
    r.on_event(ev2)
    after_second = buf.getvalue()
    assert after_second.count("Hello, how can I help?") == 1


def test_rich_does_not_suppress_distinct_bubble():
    """RichRenderer: a DIFFERENT second bubble must still render."""
    r, buf = _rich()
    r.on_turn_start("hi")

    r.on_event(_msg_step("First message.", step_n=1))
    r.on_event(_msg_step("Second message.", step_n=2))
    out = buf.getvalue()
    assert "First message." in out
    assert "Second message." in out


# ---------------------------------------------------------------------------
# State is per-turn: dedup resets between turns
# ---------------------------------------------------------------------------


def test_plain_dedup_resets_between_turns():
    """The same text shown in turn 1 must still render in turn 2."""
    r, buf = _plain()
    r.on_turn_start("turn 1")
    r.on_event(_msg_step("repeating message", step_n=1))
    r.on_turn_end("")

    r.on_turn_start("turn 2")
    r.on_event(_msg_step("repeating message", step_n=1))
    r.on_turn_end("")

    out = buf.getvalue()
    assert out.count("repeating message") == 2


def test_rich_dedup_resets_between_turns():
    """The same text shown in turn 1 must still render in turn 2."""
    r, buf = _rich()
    r.on_turn_start("turn 1")
    r.on_event(_msg_step("repeating message", step_n=1))
    r.on_turn_end("")

    r.on_turn_start("turn 2")
    r.on_event(_msg_step("repeating message", step_n=1))
    r.on_turn_end("")

    out = buf.getvalue()
    assert out.count("repeating message") == 2
