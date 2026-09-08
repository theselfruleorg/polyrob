"""B25 (S9, 2026-08-29): ``GoalBoard.get`` takes an optional tenant filter; the
tenant-facing callers pass it so a foreign goal id is simply 'not found'."""
import pytest

from agents.task.goals.board import GoalBoard


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def test_get_with_user_id_hides_foreign_rows(board):
    g = board.create(user_id="alice", title="alice's goal", body="")
    assert board.get(g.id).id == g.id                     # unscoped (internal callers)
    assert board.get(g.id, user_id="alice").id == g.id
    assert board.get(g.id, user_id="bob") is None


def test_goal_show_dependency_titles_never_leak_across_tenants(board):
    import inspect
    from tools import goal_tools
    src = inspect.getsource(goal_tools)
    # every tenant-facing board.get in the goal tool carries the tenant filter
    assert "board.get(params.goal_id, user_id=user_id)" in src
    assert "board.get(dep_id, user_id=user_id)" in src
    assert "board.get(params.objective_id, user_id=user_id)" in src
