"""An unbounded objective is what produced "x402 Round 9".

`Explore the x402 agent economy and engage other agents` accumulated 32 child
goals over a month with no completion criterion and no budget. An objective that
can never be finished forces the planner to invent the next increment forever,
so the work got counted in ROUNDS instead of outcomes — Rounds 1..9, Engagements
1..5, "recon round 2" — while the objective's actual purpose (revenue) produced
10 invoices and $0.

A budget converts "keep going" into "come back to me": past the cap the board
refuses new children for that objective and says what to do instead.
"""
import pytest

from agents.task.goals.board import GoalBoard, OBJ_ACTIVE


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _objective(board, **payload):
    return board.create_objective(user_id="rob", title="Explore the x402 economy",
                                  payload=payload or None)


def test_children_are_allowed_under_the_budget(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "3")
    obj = _objective(board)

    for i in range(3):
        board.create(user_id="rob", title=f"round {i}", parent_id=obj.id, force=True)

    assert len(board.children_of("rob", obj.id)) == 3


def test_the_budget_refuses_the_next_child(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "3")
    obj = _objective(board)
    for i in range(3):
        board.create(user_id="rob", title=f"round {i}", parent_id=obj.id, force=True)

    with pytest.raises(ValueError) as e:
        board.create(user_id="rob", title="round 4", parent_id=obj.id, force=True)

    assert "budget" in str(e.value).lower()
    assert obj.id in str(e.value)


def test_a_cancelled_child_does_not_consume_budget(board, monkeypatch):
    """The cap counts live work, not history — an abandoned goal is not spend."""
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    obj = _objective(board)
    a = board.create(user_id="rob", title="round 1", parent_id=obj.id, force=True)
    board.create(user_id="rob", title="round 2", parent_id=obj.id, force=True)
    board.cancel(a.id, user_id="rob")

    board.create(user_id="rob", title="round 3", parent_id=obj.id, force=True)


def test_a_goal_with_no_objective_is_never_budget_capped(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "1")
    board.create(user_id="rob", title="one-off a", force=True)
    board.create(user_id="rob", title="one-off b", force=True)


def test_an_objective_can_raise_its_own_budget(board, monkeypatch):
    """A deliberately long-running objective states its own number."""
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "1")
    obj = _objective(board, goal_budget=3)

    for i in range(3):
        board.create(user_id="rob", title=f"round {i}", parent_id=obj.id, force=True)

    with pytest.raises(ValueError):
        board.create(user_id="rob", title="round 4", parent_id=obj.id, force=True)


def test_budget_zero_disables_the_cap(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "0")
    obj = _objective(board)
    for i in range(6):
        board.create(user_id="rob", title=f"round {i}", parent_id=obj.id, force=True)
