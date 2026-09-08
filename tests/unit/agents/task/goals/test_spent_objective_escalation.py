"""A budget-exhausted objective escalates to the owner deterministically.

Prod 2026-08-28: two of four active objectives sat at 25/25 live goals for days.
`_check_objective_budget` refuses the child and tells the AGENT to raise an ask,
and the planner prompt repeats it — but both are prompt-shaped, so the owner only
hears about it if the model complies. It did not. These tests pin the
code-enforced path.
"""
import os

import pytest

from agents.task.goals.board import ASK_OPEN, OBJ_ACTIVE, GoalBoard


@pytest.fixture()
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


def _fill(board, uid, objective_id, n):
    for i in range(n):
        board.create(user_id=uid, title=f"child {i}", parent_id=objective_id,
                     force=True)


def test_spent_objective_raises_one_ask(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "3")
    obj = board.create_objective(user_id="u1", title="Ship the thing")
    _fill(board, "u1", obj.id, 3)

    created = board.escalate_spent_objectives(user_id="u1")

    assert len(created) == 1
    asks = board.asks(user_id="u1", status=ASK_OPEN)
    assert len(asks) == 1
    ask = asks[0]
    assert (ask.payload or {}).get("kind") == GoalBoard.ASK_KIND_OBJECTIVE_SPENT
    assert (ask.payload or {}).get("objective_id") == obj.id
    assert (ask.payload or {}).get("live") == 3
    assert (ask.payload or {}).get("budget") == 3
    # The ask must name the remedy, not just the condition.
    assert "goal_budget" in ask.body
    assert obj.id in ask.body


def test_escalation_is_idempotent(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    obj = board.create_objective(user_id="u1", title="Ship the thing")
    _fill(board, "u1", obj.id, 2)

    first = board.escalate_spent_objectives(user_id="u1")
    second = board.escalate_spent_objectives(user_id="u1")
    third = board.escalate_spent_objectives(user_id="u1")

    assert len(first) == 1
    assert second == [] and third == []
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1


def test_objective_under_budget_is_not_escalated(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "5")
    obj = board.create_objective(user_id="u1", title="Plenty of room")
    _fill(board, "u1", obj.id, 4)

    assert board.escalate_spent_objectives(user_id="u1") == []
    assert board.asks(user_id="u1", status=ASK_OPEN) == []


def test_stream_objective_is_uncapped_and_never_escalated(board, monkeypatch):
    """A stream is standing recurring work — `objective_budget` returns 0 for it,
    and a 0 budget must read as 'uncapped', never as 'instantly spent'."""
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    obj = board.create_objective(user_id="u1", title="Standing stream",
                                 payload={"stream_id": "treasury-trading"})
    _fill(board, "u1", obj.id, 6)

    assert board.objective_budget(obj) == 0
    assert board.escalate_spent_objectives(user_id="u1") == []


def test_escalation_is_tenant_scoped(board, monkeypatch):
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    mine = board.create_objective(user_id="u1", title="Mine")
    theirs = board.create_objective(user_id="u2", title="Theirs")
    _fill(board, "u1", mine.id, 2)
    _fill(board, "u2", theirs.id, 2)

    created = board.escalate_spent_objectives(user_id="u1")

    assert len(created) == 1
    assert len(board.asks(user_id="u1", status=ASK_OPEN)) == 1
    assert board.asks(user_id="u2", status=ASK_OPEN) == []


def test_cancelled_children_do_not_count_toward_the_budget(board, monkeypatch):
    """`children_of` is live-only, so cancelling work must actually free budget —
    otherwise the remedy the ask recommends would not work."""
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "2")
    obj = board.create_objective(user_id="u1", title="Ship the thing")
    _fill(board, "u1", obj.id, 2)
    kids = board.children_of("u1", obj.id)
    board.update_status(kids[0].id, "cancelled", user_id="u1")

    assert board.escalate_spent_objectives(user_id="u1") == []


def test_escalation_survives_a_broken_objective_row(board, monkeypatch):
    """Fail-open per objective: one bad row must not stop the others escalating."""
    monkeypatch.setenv("OBJECTIVE_GOAL_BUDGET", "1")
    good = board.create_objective(user_id="u1", title="Good one")
    _fill(board, "u1", good.id, 1)

    real_budget = board.objective_budget
    calls = {"n": 0}

    def flaky(objective):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated read failure")
        return real_budget(objective)

    other = board.create_objective(user_id="u1", title="Other one")
    _fill(board, "u1", other.id, 1)
    monkeypatch.setattr(board, "objective_budget", flaky)

    created = board.escalate_spent_objectives(user_id="u1")

    # One objective blew up, the other still produced its ask.
    assert len(created) == 1
