"""FIX 5 — planner stall/backoff bookkeeping must be tenant-scoped.

Every planner bookkeeping row keyed on the single constant
`GoalBoard.PLANNER_SENTINEL` ("__planner__") with no `user_id` filter, unlike
every other query in board.py. On a multi-tenant board one tenant's planner run
reset every tenant's backoff, and its escalation dedup suppressed another
tenant's stall ask. Single-owner today, so this was latent — but the fix must not
lose the CURRENT streak state on upgrade either: an unsuffixed legacy row still
counts for the tenant, so a deploy can neither crash nor silently reset the
backoff to zero (which would re-arm the escalation the durable marker exists to
stop).
"""
import pytest

from agents.task.goals.board import GoalBoard


@pytest.fixture()
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_planner_run_of_one_tenant_does_not_reset_another(board):
    board.mark_planner_run(user_id="alice")
    assert board.last_planner_run_at(user_id="alice") is not None
    assert board.last_planner_run_at(user_id="bob") is None, \
        "alice's run must not start bob's cooldown"


def test_empty_streak_is_per_tenant(board):
    board.mark_planner_outcome(queued=0, user_id="alice")
    board.mark_planner_outcome(queued=0, user_id="alice")
    assert board.consecutive_empty_planner_runs(user_id="alice") == 2
    assert board.consecutive_empty_planner_runs(user_id="bob") == 0

    board.mark_planner_outcome(queued=3, user_id="bob")
    assert board.consecutive_empty_planner_runs(user_id="alice") == 2, \
        "bob producing goals must not clear alice's backoff"


def test_stall_escalation_dedup_is_per_tenant(board):
    board.mark_planner_outcome(queued=0, live=0, user_id="alice")
    board.mark_planner_outcome(queued=0, live=0, user_id="bob")
    board.mark_stall_escalated(user_id="alice")

    assert board.stall_escalated_since(
        board.empty_streak_started_at(user_id="alice"), user_id="alice") is True
    assert board.stall_escalated_since(
        board.empty_streak_started_at(user_id="bob"), user_id="bob") is False, \
        "alice's escalation must not suppress bob's stall ask"


def test_stall_streak_is_per_tenant(board):
    board.mark_planner_outcome(queued=0, live=0, user_id="alice")
    board.mark_planner_outcome(queued=0, live=1, user_id="bob")
    assert board.consecutive_stall_runs(user_id="alice") == 1
    assert board.consecutive_stall_runs(user_id="bob") == 0


# --- upgrade continuity: unsuffixed legacy rows still count ----------------

def _legacy(board, kind, payload=None):
    """A row written by the pre-fix code (bare "__planner__", no tenant)."""
    board._event(GoalBoard.PLANNER_SENTINEL, kind, payload or {})


def test_legacy_rows_are_still_read_by_a_tenant_scoped_read(board):
    _legacy(board, "planner_outcome", {"queued": 0, "live": 0})
    _legacy(board, "planner_outcome", {"queued": 0, "live": 0})
    assert board.consecutive_empty_planner_runs(user_id="rob") == 2, \
        "an upgrade must not silently reset the backoff to zero"
    assert board.last_planner_run_at(user_id="rob") is None
    _legacy(board, "planner_run", {})
    assert board.last_planner_run_at(user_id="rob") is not None


def test_a_legacy_escalation_still_dedups_its_own_streak(board):
    _legacy(board, "planner_outcome", {"queued": 0, "live": 0})
    _legacy(board, "empty_pipeline_escalated", {})
    started = board.empty_streak_started_at(user_id="rob")
    assert board.stall_escalated_since(started, user_id="rob") is True, \
        "the once-per-stall marker must survive the upgrade (no escalation storm)"


def test_no_user_id_keeps_the_legacy_behaviour(board):
    """Back-compat: the un-scoped calls still work exactly as before."""
    board.mark_planner_outcome(queued=0)
    assert board.consecutive_empty_planner_runs() == 1
    board.mark_planner_run()
    assert board.last_planner_run_at() is not None
