"""Tests for cli.ui.lifecycle.TurnLifecycle — the CLI's single source of truth
for active-turn + work-clock + derived status word.

Design ref: docs/plans/2026-06-26-session-runtime-lifecycle-PLAN.md §1.

The clock is injectable so every timing assertion is deterministic (no sleeps).
"""

import pytest

from cli.ui.lifecycle import Phase, TurnLifecycle, TurnOutcome


class FakeClock:
    """A monotonic clock you advance by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _lc() -> tuple[TurnLifecycle, FakeClock]:
    clk = FakeClock()
    return TurnLifecycle(clock=clk), clk


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_starts_idle_ready_clock_frozen():
    lc, _ = _lc()
    assert lc.phase is Phase.IDLE
    assert lc.is_active() is False
    assert lc.active_elapsed() == 0.0
    assert lc.status_word() == "ready"
    assert lc.autonomy_busy() is False


# ---------------------------------------------------------------------------
# Foreground turn: begin / clock / end
# ---------------------------------------------------------------------------


def test_begin_turn_goes_active_and_clock_runs():
    lc, clk = _lc()
    tok = lc.begin_turn()
    assert isinstance(tok, int)
    assert lc.is_active() is True
    assert lc.status_word() == "working"
    assert lc.active_elapsed() == 0.0
    clk.advance(3.5)
    assert lc.active_elapsed() == pytest.approx(3.5)


def test_end_turn_ok_freezes_clock_and_returns_ready():
    lc, clk = _lc()
    tok = lc.begin_turn()
    clk.advance(5.0)
    lc.end_turn(tok, TurnOutcome.OK)
    assert lc.is_active() is False
    assert lc.status_word() == "ready"
    # Clock frozen on idle — does NOT keep counting while the user sits there.
    assert lc.active_elapsed() == 0.0
    clk.advance(3600.0)
    assert lc.active_elapsed() == 0.0  # still frozen (this is the "1h26m" bug fix)


def test_default_outcome_is_ok():
    lc, clk = _lc()
    tok = lc.begin_turn()
    lc.end_turn(tok)  # no explicit outcome
    assert lc.status_word() == "ready"


# ---------------------------------------------------------------------------
# Outcomes: ERROR / CANCELLED are sticky until the next begin_turn
# ---------------------------------------------------------------------------


def test_error_outcome_is_sticky():
    lc, _ = _lc()
    tok = lc.begin_turn()
    lc.end_turn(tok, TurnOutcome.ERROR)
    assert lc.status_word() == "error"
    # Stays until the next turn begins.
    assert lc.status_word() == "error"


def test_cancelled_outcome_is_stopped():
    lc, _ = _lc()
    tok = lc.begin_turn()
    lc.end_turn(tok, TurnOutcome.CANCELLED)
    assert lc.status_word() == "stopped"


def test_begin_turn_clears_prior_outcome():
    lc, _ = _lc()
    tok = lc.begin_turn()
    lc.end_turn(tok, TurnOutcome.ERROR)
    assert lc.status_word() == "error"
    lc.begin_turn()
    assert lc.status_word() == "working"
    assert lc.last_outcome is TurnOutcome.NONE


# ---------------------------------------------------------------------------
# Idempotent begin (dup/race) must NOT reset the clock
# ---------------------------------------------------------------------------


def test_begin_while_active_is_noop_and_keeps_clock():
    lc, clk = _lc()
    tok1 = lc.begin_turn()
    clk.advance(4.0)
    tok2 = lc.begin_turn()  # duplicate begin (race)
    assert tok2 == tok1  # same token — not a new turn
    assert lc.active_elapsed() == pytest.approx(4.0)  # clock NOT reset


# ---------------------------------------------------------------------------
# Turn token defeats the cancel→resubmit race
# ---------------------------------------------------------------------------


def test_stale_end_turn_is_ignored():
    lc, clk = _lc()
    tok_old = lc.begin_turn()
    # Cancelled + immediately resubmitted: a new turn starts before the old
    # task's end_turn lands.
    lc.end_turn(tok_old, TurnOutcome.CANCELLED)  # old turn settles
    tok_new = lc.begin_turn()  # resubmit → new ACTIVE turn
    assert tok_new != tok_old
    clk.advance(2.0)
    # The OLD task's late settle must NOT touch the new active turn.
    lc.end_turn(tok_old, TurnOutcome.CANCELLED)
    assert lc.is_active() is True
    assert lc.status_word() == "working"
    assert lc.active_elapsed() == pytest.approx(2.0)


def test_end_turn_while_idle_is_noop():
    lc, _ = _lc()
    lc.end_turn(0, TurnOutcome.OK)  # never began
    assert lc.phase is Phase.IDLE
    assert lc.status_word() == "ready"


def test_end_turn_with_current_token_after_idle_does_not_revive():
    lc, _ = _lc()
    tok = lc.begin_turn()
    lc.end_turn(tok, TurnOutcome.OK)
    # Re-applying the same (now-stale) token must not flip state.
    lc.end_turn(tok, TurnOutcome.ERROR)
    assert lc.status_word() == "ready"  # NOT "error"


# ---------------------------------------------------------------------------
# Autonomy lane — orthogonal; never touches the work clock or status word
# ---------------------------------------------------------------------------


def test_autonomy_is_orthogonal_to_foreground():
    lc, clk = _lc()
    lc.begin_background()
    assert lc.autonomy_busy() is True
    # Foreground untouched: still idle, clock frozen, word ready.
    assert lc.is_active() is False
    assert lc.status_word() == "ready"
    assert lc.active_elapsed() == 0.0
    lc.end_background()
    assert lc.autonomy_busy() is False


def test_autonomy_overlaps_foreground_turn():
    lc, clk = _lc()
    tok = lc.begin_turn()
    clk.advance(2.0)
    lc.begin_background()
    # Both lanes live: foreground working with a running clock, autonomy busy.
    assert lc.is_active() is True
    assert lc.status_word() == "working"
    assert lc.active_elapsed() == pytest.approx(2.0)
    assert lc.autonomy_busy() is True
    # Ending the user turn does not clear autonomy.
    lc.end_turn(tok, TurnOutcome.OK)
    assert lc.autonomy_busy() is True
    assert lc.status_word() == "ready"


def test_autonomy_counter_is_floored():
    lc, _ = _lc()
    lc.end_background()  # underflow guard
    assert lc.autonomy_busy() is False
    lc.begin_background()
    lc.begin_background()
    lc.end_background()
    assert lc.autonomy_busy() is True  # still one in flight
    lc.end_background()
    assert lc.autonomy_busy() is False


# ---------------------------------------------------------------------------
# Planning-only turn (no done event) settles on end_turn — the free latent fix
# ---------------------------------------------------------------------------


def test_planning_only_turn_settles_to_ready():
    # The lifecycle settles on the turn runner calling end_turn (i.e. respond()
    # returning), NOT on any feed "done" event — so a turn that ends on a
    # planning step with no final message still returns to idle.
    lc, _ = _lc()
    tok = lc.begin_turn()
    lc.end_turn(tok, TurnOutcome.OK)
    assert lc.status_word() == "ready"
    assert lc.is_active() is False
