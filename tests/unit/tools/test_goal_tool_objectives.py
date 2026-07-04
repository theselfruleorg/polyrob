"""Task 6: objective/update actions + acceptance + autonomy gate on the goal tool.

Covers ObjectiveAdd/List/SetStatus, goal_update, goal_create's objective_id/acceptance
fields + distinct duplicate-error message, and the autonomy gate refusing mutation from
an autonomous (goal/cron-spawned) session — including via parent_session_id for a
virtual sub-agent session.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals.autonomy_marker import mark_autonomous
from agents.task.goals.board import GoalBoard, OBJ_PAUSED
from tools.goal_tools import (
    GoalCreateAction, GoalTool, GoalUpdateAction,
    ObjectiveAddAction, ObjectiveListAction, ObjectiveSetStatusAction,
)


def _tool(tmp_path):
    t = GoalTool.__new__(GoalTool)
    t._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return t


def _ctx(session_id="owner-sess", user_id="rob"):
    return SimpleNamespace(session_id=session_id, parent_session_id=None, user_id=user_id)


def run(coro):
    return asyncio.run(coro)


def test_objective_add_and_list(tmp_path):
    t = _tool(tmp_path)
    r = run(t.objective_add(ObjectiveAddAction(title="Get 100k followers"), _ctx()))
    assert not r.error and "objective" in r.extracted_content.lower()
    r2 = run(t.objective_list(ObjectiveListAction(), _ctx()))
    assert "Get 100k followers" in r2.extracted_content


def test_objective_set_status(tmp_path):
    t = _tool(tmp_path)
    o = t._goal_board.create_objective(user_id="rob", title="Earn money")
    r = run(t.objective_set_status(
        ObjectiveSetStatusAction(objective_id=o.id, status="pause"), _ctx()))
    assert not r.error
    assert t._goal_board.get(o.id).status == OBJ_PAUSED


def test_goal_create_with_objective_and_acceptance(tmp_path):
    t = _tool(tmp_path)
    o = t._goal_board.create_objective(user_id="rob", title="Earn money")
    r = run(t.goal_create(GoalCreateAction(
        title="Draft substack pricing post", objective_id=o.id,
        acceptance="a new file in project/ with a pricing table"), _ctx()))
    assert not r.error
    g = t._goal_board.children(o.id)[0]
    assert g.parent_id == o.id
    assert g.payload["acceptance"].startswith("a new file")


def test_goal_create_rejects_bad_objective(tmp_path):
    t = _tool(tmp_path)
    r = run(t.goal_create(GoalCreateAction(title="X goal here", objective_id="nope"), _ctx()))
    assert r.error and "objective" in r.error.lower()


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
    o = t._goal_board.create_objective(user_id="rob", title="Earn money")
    mark_autonomous("goal-sess-1")
    ctx = _ctx(session_id="goal-sess-1")
    for coro in (
        t.objective_add(ObjectiveAddAction(title="Sneaky new mission"), ctx),
        t.objective_set_status(ObjectiveSetStatusAction(objective_id=o.id, status="drop"), ctx),
        t.goal_update(GoalUpdateAction(goal_id=o.id, priority=1), ctx),
    ):
        r = run(coro)
        assert r.error and "autonomous" in r.error.lower()
    # reads + create stay allowed
    assert not run(t.objective_list(ObjectiveListAction(), ctx)).error
    assert not run(t.goal_create(GoalCreateAction(title="A brand new unique goal"), ctx)).error


def test_autonomous_subagent_blocked_via_parent(tmp_path):
    t = _tool(tmp_path)
    mark_autonomous("goal-sess-2")
    ctx = SimpleNamespace(session_id="virtual-sub", parent_session_id="goal-sess-2", user_id="rob")
    r = run(t.objective_add(ObjectiveAddAction(title="Sub sneaky"), ctx))
    assert r.error and "autonomous" in r.error.lower()
