"""Integration tests for the TurnLifecycle wiring into the CLI (Phases 1-2).

Covers:
- the status bar shows the ACTIVE turn's work clock and HIDES it on idle (the
  "1h26m while idle" fix);
- ``run_turn`` begins/ends the lifecycle around a turn, with the right outcome
  on success / error / cancel, exactly once (try/finally + token).

Clocks are injected so timing is deterministic.
"""

import asyncio

import pytest

from cli.ui import statusbar
from cli.ui.lifecycle import Phase, TurnOutcome
from cli.ui.state import SessionState


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# Status bar work clock
# ---------------------------------------------------------------------------


def test_bar_hides_elapsed_when_idle():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    clk.advance(5000.0)  # session age grows hugely while idle
    out = statusbar.status_text(s)
    # No work clock shown at the prompt — session age must not leak as "elapsed".
    assert "s" not in out.split("·")[-1].strip().rstrip("s") or "5000" not in out
    assert "1h" not in out and "83m" not in out


def test_bar_shows_active_work_clock():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    s.lifecycle.begin_turn()
    clk.advance(7.0)
    out = statusbar.status_text(s)
    assert "7s" in out


def test_bar_clock_freezes_after_turn_ends():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    tok = s.lifecycle.begin_turn()
    clk.advance(7.0)
    s.lifecycle.end_turn(tok, TurnOutcome.OK)
    clk.advance(3600.0)  # user sits idle for an hour
    out = statusbar.status_text(s)
    assert "1h" not in out  # frozen + hidden, not session age


def test_formatted_bar_hides_elapsed_when_idle():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    clk.advance(5000.0)
    ft = statusbar.status_formatted(s)
    classes = [c for c, _ in ft]
    assert "class:toolbar.elapsed" not in classes


def test_formatted_bar_shows_elapsed_when_active():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    s.lifecycle.begin_turn()
    clk.advance(3.0)
    ft = statusbar.status_formatted(s)
    classes = [c for c, _ in ft]
    assert "class:toolbar.elapsed" in classes


# ---------------------------------------------------------------------------
# run_turn drives the lifecycle (success / error / cancel) exactly once
# ---------------------------------------------------------------------------


class _FakeRenderer:
    def __init__(self, state: SessionState) -> None:
        self.state = state
        self.events = []

    def on_turn_start(self, line: str) -> None:
        self.events.append(("start", line))

    def on_turn_end(self, answer: str) -> None:
        self.events.append(("end", answer))

    def on_event(self, event) -> None:
        self.events.append(("event", event))


class _OkConvo:
    async def respond(self, line: str) -> str:
        return "ok answer"


class _BoomConvo:
    async def respond(self, line: str) -> str:
        raise RuntimeError("boom")


class _CancelConvo:
    async def respond(self, line: str) -> str:
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_run_turn_success_settles_ready():
    from cli.ui.persistent_loop import run_turn

    s = SessionState()
    r = _FakeRenderer(s)
    await run_turn(_OkConvo(), "hi", r)
    assert s.lifecycle.phase is Phase.IDLE
    assert s.lifecycle.status_word() == "ready"
    assert s.lifecycle.last_outcome is TurnOutcome.NONE


@pytest.mark.asyncio
async def test_run_turn_error_is_sticky_error():
    from cli.ui.persistent_loop import run_turn

    s = SessionState()
    r = _FakeRenderer(s)
    await run_turn(_BoomConvo(), "hi", r)
    assert s.lifecycle.phase is Phase.IDLE
    assert s.lifecycle.status_word() == "error"


@pytest.mark.asyncio
async def test_run_turn_cancel_is_stopped():
    from cli.ui.persistent_loop import run_turn

    s = SessionState()
    r = _FakeRenderer(s)
    await run_turn(_CancelConvo(), "hi", r)
    assert s.lifecycle.phase is Phase.IDLE
    assert s.lifecycle.status_word() == "stopped"


def test_cooking_affordance_while_active():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    s.lifecycle.begin_turn()
    clk.advance(7.0)
    out = statusbar.status_text(s)
    assert "cooking…" in out
    assert "7s" in out
    # Idle word is not shown mid-turn.
    assert "ready" not in out


def test_cooking_affordance_uses_spinner_glyph_when_supplied():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    s.lifecycle.begin_turn()
    clk.advance(2.0)
    out = statusbar.status_text(s, spinner="⠙ ")
    assert "⠙ cooking… 2s" in out


def test_idle_shows_status_word_not_cooking():
    s = SessionState()
    s.model = "m"
    # After a completed turn the lifecycle is idle/ready (status synced at the seam).
    tok = s.lifecycle.begin_turn()
    s.lifecycle.end_turn(tok)
    s.status = s.lifecycle.status_word()  # what sync_status does at the seam
    out = statusbar.status_text(s)
    assert "cooking" not in out
    assert "ready" in out


def test_background_feed_events_cannot_flip_idle_status():
    """Issue B regression: while the user is idle, feed events (as a background
    autonomy turn would emit) must NOT flip the bar to 'working'/'running'."""
    from cli.ui.events import LLMCall, SessionStart, Step

    s = SessionState()
    # Simulate a stream of feed events with no user turn in flight.
    s.update(SessionStart(model_name="m", agent_id="x"))
    s.update(Step(step=1, raw={"data": {"agent_name": "rob"}}))
    s.update(LLMCall(prompt_tokens=10, completion_tokens=5, token_count=15))
    # The lifecycle stays idle; the derived word is never 'working'.
    assert s.lifecycle.is_active() is False
    assert s.lifecycle.status_word() == "ready"


def test_autonomy_segment_shown_only_when_background_busy():
    s = SessionState()
    s.model = "m"
    assert "autonomy" not in statusbar.status_text(s)
    s.lifecycle.begin_background()
    assert "⟲ autonomy" in statusbar.status_text(s)
    s.lifecycle.end_background()
    assert "autonomy" not in statusbar.status_text(s)


def test_autonomy_does_not_start_the_work_clock_or_status():
    clk = FakeClock()
    s = SessionState(clock=clk)
    s.model = "m"
    s.lifecycle.begin_background()
    clk.advance(50.0)
    # The user's work clock never started; the status word stays idle 'ready'.
    out = statusbar.status_text(s)
    assert "50s" not in out
    assert s.lifecycle.status_word() == "ready"
    assert s.lifecycle.is_active() is False


def test_autonomy_segment_in_formatted_bar():
    s = SessionState()
    s.model = "m"
    s.lifecycle.begin_background()
    classes = [c for c, _ in statusbar.status_formatted(s)]
    assert "class:toolbar.autonomy" in classes


def test_spinner_shows_only_when_lifecycle_active():
    from cli.ui import app as app_mod

    s = SessionState()
    s.model = "m"
    frames = app_mod.SPINNER_FRAMES

    # Idle: no spinner glyph in the toolbar.
    idle_text = "".join(t for _, t in app_mod.make_bottom_toolbar(s, clock=lambda: 0.0)())
    assert not any(f in idle_text for f in frames)

    # Active turn: the spinner glyph appears.
    s.lifecycle.begin_turn()
    active_text = "".join(t for _, t in app_mod.make_bottom_toolbar(s, clock=lambda: 0.0)())
    assert any(f in active_text for f in frames)


@pytest.mark.asyncio
async def test_run_turn_clock_runs_then_freezes():
    from cli.ui.persistent_loop import run_turn

    clk = FakeClock()
    s = SessionState(clock=clk)
    r = _FakeRenderer(s)

    class _SlowConvo:
        async def respond(self, line: str) -> str:
            clk.advance(4.0)  # simulate work during the turn
            return "done"

    await run_turn(_SlowConvo(), "hi", r)
    # After the turn the clock is frozen/hidden regardless of further idle time.
    clk.advance(999.0)
    assert s.lifecycle.active_elapsed() == 0.0
