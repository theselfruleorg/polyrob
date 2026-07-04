import pytest

from agents.task.goals.board import GoalBoard


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _claimed(board):
    g = board.create(user_id="rob", title="claimed goal")
    board.claim(g.id, "w1", ttl_seconds=60)
    return g


def test_cancel_survives_record_success(board):
    g = _claimed(board)
    board.cancel(g.id)
    board.record_success(g.id, session_id="s1", result="finished anyway")
    assert board.get(g.id).status == "cancelled"
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "stale_completion" in kinds and "succeeded" not in kinds


def test_cancel_survives_record_failure_no_resurrect(board):
    g = _claimed(board)
    board.cancel(g.id)
    out = board.record_failure(g.id, error="boom")
    assert out.status == "cancelled"
    assert out.consecutive_failures == 0  # untouched


def test_pause_blocked_survives_record_success(board):
    g = _claimed(board)
    board.update_status(g.id, "blocked")
    board.record_success(g.id, result="late")
    assert board.get(g.id).status == "blocked"


def test_running_goal_still_completes_normally(board):
    g = _claimed(board)
    board.record_success(g.id, session_id="s1", result="ok")
    assert board.get(g.id).status == "done"


def test_running_goal_still_fails_normally(board):
    g = _claimed(board)
    out = board.record_failure(g.id, error="e1")
    assert out.status == "ready" and out.consecutive_failures == 1


def test_record_failure_unknown_id_still_raises(board):
    with pytest.raises(KeyError):
        board.record_failure("nope", error="e")


def test_record_failure_normal_running_goal_unchanged(board):
    """Regression guard for the 'AND status=running' hardening on the branch
    UPDATEs: the common (non-raced) path must behave exactly as before."""
    g = _claimed(board)
    out = board.record_failure(g.id, error="boom")
    assert out.status == "ready"
    assert out.consecutive_failures == 1
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "failed" in kinds and "stale_completion" not in kinds


def test_record_failure_on_already_cancelled_goal_does_not_resurrect(board):
    """A goal whose status was flipped to 'cancelled' (owner intervened) after the
    counter was pre-incremented some other way must not be revived to 'ready' by a
    second record_failure — the branch UPDATE's own 'AND status=running' guard must
    hold even when the row is no longer 'running' at branch-decision time."""
    g = _claimed(board)
    board.update_status(g.id, "cancelled")
    out = board.record_failure(g.id, error="boom-after-cancel")
    assert out.status == "cancelled"  # not resurrected to 'ready' or 'blocked'
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "stale_completion" in kinds
    assert "failed" not in kinds and "gave_up" not in kinds
