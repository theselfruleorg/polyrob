"""Task 6: update action + acceptance + autonomy gate on the goal tool.

Covers goal_update, goal_create's distinct duplicate-error message, and the
autonomy gate refusing mutation from
an autonomous (goal/cron-spawned) session — including via parent_session_id for a
virtual sub-agent session.
"""
import asyncio
from types import SimpleNamespace

import pytest

from core.autonomy_marker import mark_autonomous
from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalCreateAction, GoalTool, GoalUpdateAction


def _tool(tmp_path):
    t = GoalTool.__new__(GoalTool)
    t._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return t


def _ctx(session_id="owner-sess", user_id="rob"):
    return SimpleNamespace(session_id=session_id, parent_session_id=None, user_id=user_id,
                           role="orchestrator", is_sub_agent=False, metadata={})


@pytest.fixture(autouse=True)
def bound_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")


def run(coro):
    return asyncio.run(coro)


def test_goal_create_duplicate_returns_match(tmp_path):
    t = _tool(tmp_path)
    run(t.goal_create(GoalCreateAction(title="Research concrete revenue angles"), _ctx()))
    r = run(t.goal_create(GoalCreateAction(title="Research revenue angles concrete"), _ctx()))
    assert r.error and "duplicate" in r.error.lower()


def test_goal_update(tmp_path):
    t = _tool(tmp_path)
    g = t._goal_board.create(user_id="rob", title="Old title words")
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, priority=8,
                                           acceptance="tweet id posted"), _ctx()))
    assert not r.error
    g2 = t._goal_board.get(g.id)
    assert g2.priority == 8 and g2.payload["acceptance"] == "tweet id posted"


def test_autonomous_session_cannot_mutate(tmp_path):
    t = _tool(tmp_path)
    g = t._goal_board.create(user_id="rob", title="Earn money")
    mark_autonomous("goal-sess-1")
    ctx = _ctx(session_id="goal-sess-1")
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, priority=1), ctx))
    assert r.error and "autonomous" in r.error.lower()
    # create stays allowed
    assert not run(t.goal_create(GoalCreateAction(title="A brand new unique goal"), ctx)).error


def test_autonomous_subagent_blocked_via_parent(tmp_path):
    t = _tool(tmp_path)
    mark_autonomous("goal-sess-2")
    ctx = SimpleNamespace(session_id="virtual-sub", parent_session_id="goal-sess-2", user_id="rob")
    g = t._goal_board.create(user_id="rob", title="Owner standing goal")
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, priority=1), ctx))
    assert r.error and "autonomous" in r.error.lower()


@pytest.mark.parametrize("change", [
    {"role": "leaf"}, {"is_sub_agent": True},
    {"metadata": {"turn_kind": "self_wake"}},
    {"metadata": {"turn_kind": "group"}}, {"user_id": "stranger"},
])
def test_owner_identity_alone_does_not_authorize_goal_mutation(tmp_path, change):
    tool = _tool(tmp_path)
    goal = tool._goal_board.create(user_id="rob", title="Owner's standing work")
    ctx = _ctx()
    for key, value in change.items():
        setattr(ctx, key, value)
    result = run(tool.goal_update(GoalUpdateAction(goal_id=goal.id, priority=10), ctx))
    assert result.error
    assert tool._goal_board.get(goal.id).priority != 10
