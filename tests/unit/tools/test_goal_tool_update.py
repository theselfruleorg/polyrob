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


def _ctx(session_id="owner-sess", user_id="alice"):
    return SimpleNamespace(session_id=session_id, parent_session_id=None, user_id=user_id,
                           role="orchestrator", is_sub_agent=False, metadata={})


@pytest.fixture(autouse=True)
def bound_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")


def run(coro):
    return asyncio.run(coro)


def test_goal_create_duplicate_returns_match(tmp_path):
    t = _tool(tmp_path)
    run(t.goal_create(GoalCreateAction(title="Research concrete revenue angles"), _ctx()))
    r = run(t.goal_create(GoalCreateAction(title="Research revenue angles concrete"), _ctx()))
    assert r.error and "duplicate" in r.error.lower()


def test_goal_update(tmp_path):
    t = _tool(tmp_path)
    g = t._goal_board.create(user_id="alice", title="Old title words")
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, priority=8,
                                           acceptance="tweet id posted"), _ctx()))
    assert not r.error
    g2 = t._goal_board.get(g.id)
    assert g2.priority == 8 and g2.payload["acceptance"] == "tweet id posted"


def test_autonomous_session_cannot_mutate(tmp_path):
    t = _tool(tmp_path)
    g = t._goal_board.create(user_id="alice", title="Earn money")
    mark_autonomous("goal-sess-1")
    ctx = _ctx(session_id="goal-sess-1")
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, priority=1), ctx))
    assert r.error and "refused" in r.error.lower()
    # create stays allowed
    assert not run(t.goal_create(GoalCreateAction(title="A brand new unique goal"), ctx)).error


def test_autonomous_subagent_blocked_via_parent(tmp_path):
    t = _tool(tmp_path)
    mark_autonomous("goal-sess-2")
    ctx = SimpleNamespace(session_id="virtual-sub", parent_session_id="goal-sess-2", user_id="alice")
    g = t._goal_board.create(user_id="alice", title="Owner standing goal")
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, priority=1), ctx))
    assert r.error and "refused" in r.error.lower()


@pytest.mark.parametrize("change", [
    {"role": "leaf"}, {"is_sub_agent": True},
    {"metadata": {"turn_kind": "self_wake"}},
    {"metadata": {"turn_kind": "group"}}, {"user_id": "stranger"},
])
def test_owner_identity_alone_does_not_authorize_goal_mutation(tmp_path, change):
    tool = _tool(tmp_path)
    goal = tool._goal_board.create(user_id="alice", title="Owner's standing work")
    ctx = _ctx()
    for key, value in change.items():
        setattr(ctx, key, value)
    result = run(tool.goal_update(GoalUpdateAction(goal_id=goal.id, priority=10), ctx))
    assert result.error
    assert tool._goal_board.get(goal.id).priority != 10


def test_a_standing_run_steers_another_goal_but_never_its_own(tmp_path, monkeypatch):
    import core.security.owner_turn as O
    t = _tool(tmp_path)
    own = t._goal_board.create(user_id="alice", title="The goal this run runs")
    other = t._goal_board.create(user_id="alice", title="Some other goal")
    mark_autonomous("goal-sess-own", own.id)
    monkeypatch.setattr(O, "is_owner_standing_run", lambda ctx: True)
    ctx = _ctx(session_id="goal-sess-own")
    assert run(t.goal_update(GoalUpdateAction(goal_id=own.id, priority=1), ctx)).error
    assert not run(t.goal_update(GoalUpdateAction(goal_id=other.id, priority=1), ctx)).error


def test_goal_update_checks_a_tier_against_the_goals_own_provider(tmp_path, monkeypatch):
    """112: goal_create checks a tier against the goal's stored provider; goal_update
    asked with none, so a tier was checked against the default provider instead."""
    import tools.goal_tools as gt
    seen = []

    def _tier_payload(tier, why, owner_turn, provider=None):
        seen.append(provider)
        return {"tier": tier, "tier_why": why}, None
    monkeypatch.setattr(gt, "tier_payload", _tier_payload)
    t = _tool(tmp_path)
    g = t._goal_board.create(user_id="alice", title="Judge the payout plan",
                             payload={"provider": "provider-on-the-goal"})
    r = run(t.goal_update(GoalUpdateAction(goal_id=g.id, tier="heavy", why="decides money"),
                          _ctx()))
    assert not r.error
    assert seen == ["provider-on-the-goal"]
