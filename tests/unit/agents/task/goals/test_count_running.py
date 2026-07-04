"""Regression: GoalBoard.count_running is the cross-process in-flight count used
to enforce GOAL_MAX_CONCURRENT under workers>1 (per-process self._inflight can't
see other workers)."""
from agents.task.goals.board import GoalBoard


def _board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_count_running_reflects_claims(tmp_path):
    board = _board(tmp_path)
    g1 = board.create(user_id="u", title="a", body="a")
    g2 = board.create(user_id="u", title="b", body="b")
    assert board.count_running() == 0
    board.claim(g1.id, "w1", ttl_seconds=900)
    assert board.count_running() == 1
    board.claim(g2.id, "w2", ttl_seconds=900)
    assert board.count_running() == 2


def test_count_running_excludes_completed(tmp_path):
    board = _board(tmp_path)
    g1 = board.create(user_id="u", title="a", body="a")
    board.claim(g1.id, "w1", ttl_seconds=900)
    assert board.count_running() == 1
    board.record_success(g1.id, session_id="s1", result="ok")
    assert board.count_running() == 0
