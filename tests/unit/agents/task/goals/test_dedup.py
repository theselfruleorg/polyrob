import pytest

from agents.task.goals.board import (
    DuplicateGoalError, GoalBoard, normalize_title, title_similarity,
)


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_normalize_title():
    assert normalize_title("  Research: POLYROB revenue-angles!! ") == "research polyrob revenue angles"


def test_similarity_bounds():
    assert title_similarity("abc", "abc") == 1.0
    assert title_similarity("abc", "xyz qq") < 0.2


def test_near_duplicate_rejected(board):
    g = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    with pytest.raises(DuplicateGoalError) as ei:
        board.create(user_id="rob", title="Research revenue angles for POLYROB enterprise")
    assert ei.value.match_id == g.id
    assert ei.value.similarity >= 0.6


def test_distinct_title_accepted(board):
    board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    g2 = board.create(user_id="rob", title="Fix the substack welcome email sequence")
    assert g2.id


def test_force_overrides(board):
    board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    g2 = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB",
                      force=True)
    assert g2.id


def test_cancelled_goals_do_not_block(board):
    g = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    board.cancel(g.id)
    g2 = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    assert g2.id


def test_window_7_days(tmp_path):
    now = [1_000_000.0]
    b = GoalBoard(str(tmp_path / "g.db"), clock=lambda: now[0])
    b.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    now[0] += 8 * 86400
    g2 = b.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    assert g2.id


def test_tenant_isolation(board):
    board.create(user_id="alice", title="Research concrete revenue angles for POLYROB")
    g2 = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    assert g2.id


def test_threshold_zero_disables(board, monkeypatch):
    monkeypatch.setenv("GOAL_DEDUP_THRESHOLD", "0")
    board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    g2 = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    assert g2.id


def test_dedup_rejected_event_logged_on_match(board):
    g = board.create(user_id="rob", title="Research concrete revenue angles for POLYROB")
    with pytest.raises(DuplicateGoalError):
        board.create(user_id="rob", title="Research revenue angles for POLYROB enterprise")
    kinds = [e["kind"] for e in board.events(g.id)]
    assert "dedup_rejected" in kinds


def test_child_goal_not_deduped_against_its_parent_objective(board):
    # B12: a child goal that advances an objective must NOT be rejected as a
    # duplicate of that objective (its own parent) — else the goal is never inserted
    # and the objective can never be worked on. The goal's own parent_id is excluded
    # from dedup candidates.
    obj = board.create(user_id="rob", title="Grow the POLYROB Twitter audience",
                       kind="objective")
    child = board.create(user_id="rob",
                         title="Grow the POLYROB Twitter audience with daily posts",
                         parent_id=obj.id)
    assert child.id  # not rejected against its parent objective


def test_unrelated_near_duplicate_still_rejected_cross_kind(board):
    # Cross-kind dedup for UNRELATED items (no parent link) is intended product
    # behavior (see test_objective_add_duplicate_and_force) and must be preserved.
    board.create(user_id="rob", title="Grow the substack audience")
    with pytest.raises(DuplicateGoalError):
        board.create(user_id="rob", title="Grow the substack audience", kind="objective")
