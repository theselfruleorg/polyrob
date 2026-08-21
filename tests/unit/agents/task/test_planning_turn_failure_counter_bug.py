"""2026-08-18: a planning-turn result was wiping consecutive_failures, letting the
thinking-loop doom cycle run forever without ever tripping _too_many_failures().

Root cause: step_execution.py's ALLOWED_REASONING_TURNS allowance sets an
error-free `_last_result` (tagged `metadata.planning_turn`) for a bounded
tool-free turn. run_loop.py's "reset consecutive_failures on a successful step"
check only tested `not any(r.error ...)` — true for a planning turn too — so a
model alternating [planning turn (resets to 0), thinking-loop intervention
(bumps to 1)] never accumulated failures: the intervention's own +1 was wiped
by the very next planning turn, every cycle. Confirmed as the dominant failure
mode of the 2026-08-18 post-outage recovery burst (82% of goal failures shared
this exact signature; several runs never recovered even once, straight through
to step exhaustion) — see the 2026-08-18 intel assessment addendum.
"""
from types import SimpleNamespace

from agents.task.agent.core.conversational_exit import is_planning_turn_only_step


def _result(error=None, metadata=None):
    return SimpleNamespace(error=error, metadata=metadata or {})


def test_planning_turn_result_is_recognized():
    results = [_result(metadata={"planning_turn": True})]
    assert is_planning_turn_only_step(results) is True


def test_thinking_loop_intervention_result_is_not_a_planning_turn():
    """The intervention path already sets .error — must not be misclassified."""
    results = [_result(error="Thinking loop detected - guidance injected.")]
    assert is_planning_turn_only_step(results) is False


def test_genuinely_productive_result_is_not_a_planning_turn():
    results = [_result(metadata={"some_other_tag": True})]
    assert is_planning_turn_only_step(results) is False


def test_empty_results_is_not_a_planning_turn():
    assert is_planning_turn_only_step([]) is False
    assert is_planning_turn_only_step(None) is False


def test_mixed_results_with_one_real_action_is_not_planning_turn_only():
    """A step with ANY non-planning-turn result is genuine — must not be
    misclassified just because one of several results happens to be tagged."""
    results = [_result(metadata={"planning_turn": True}), _result(metadata={})]
    assert is_planning_turn_only_step(results) is False


def test_the_doom_cycle_scenario_end_to_end():
    """Simulates the exact bug: alternating planning-turn and intervention
    results must leave consecutive_failures monotonically non-decreasing
    across a full cycle (was: bounces 1 -> 0 -> 1 -> 0 forever)."""
    consecutive_failures = 0

    def reset_check(last_result):
        nonlocal consecutive_failures
        if (last_result and not is_planning_turn_only_step(last_result)
                and not any(r.error for r in last_result)):
            consecutive_failures = 0

    def intervention_fires():
        nonlocal consecutive_failures
        consecutive_failures += 1

    planning_turn = [_result(metadata={"planning_turn": True})]
    intervention = [_result(error="Thinking loop detected - guidance injected.")]

    # Cycle 1
    reset_check(planning_turn)     # was: consecutive_failures -> 0 (no-op, already 0)
    assert consecutive_failures == 0
    intervention_fires()
    reset_check(intervention)      # error present -> must NOT reset
    assert consecutive_failures == 1

    # Cycle 2 — the fix means this must NOT wipe the failure back to 0.
    reset_check(planning_turn)
    assert consecutive_failures == 1, (
        "a planning turn reset consecutive_failures — the doom-cycle bug is back"
    )
    intervention_fires()
    reset_check(intervention)
    assert consecutive_failures == 2

    # Cycle 3 — confirms it keeps climbing toward max_failures instead of
    # oscillating between 0 and 1 forever.
    reset_check(planning_turn)
    assert consecutive_failures == 2
    intervention_fires()
    reset_check(intervention)
    assert consecutive_failures == 3


def test_run_loop_uses_the_planning_turn_guard():
    import inspect

    from agents.task.agent.core import run_loop

    src = inspect.getsource(run_loop)
    assert "is_planning_turn_only_step" in src
