"""080 D4 — the agent's goal_cancel is the SAME stop as the owner's: once by
default; forever only in a genuine owner turn."""
import types

import pytest

from agents.task.goals.board import GoalBoard
from tools import goal_tools as T


@pytest.fixture
def tool(tmp_path, monkeypatch):
    board = GoalBoard(str(tmp_path / "goals.db"))
    t = T.GoalTool.__new__(T.GoalTool)
    monkeypatch.setattr(T.GoalTool, "_resolve_board", lambda self: board)
    monkeypatch.setattr(T.GoalTool, "_user", lambda self, ctx: "alice")
    monkeypatch.setattr(T, "_autonomy_refusal", lambda ctx, goal_id=None: None)
    return t, board


@pytest.mark.asyncio
async def test_goal_cancel_stops_once(tool, monkeypatch):
    from core.goal_suppressions import blocking
    t, board = tool
    g = board.create(user_id="alice", title="gas watch")
    res = await T.GoalTool.goal_cancel(t, T.GoalCancelAction(goal_id=g.id), None)
    assert not res.error and board.get(g.id).status == "cancelled"
    assert not blocking(board.db_path, user_id="alice", title="gas watch")


@pytest.mark.asyncio
async def test_goal_cancel_forever_needs_an_owner_turn(tool, monkeypatch):
    from core.goal_suppressions import blocking
    t, board = tool
    g = board.create(user_id="alice", title="gas watch")
    monkeypatch.setattr(T, "owner_seat_turn", lambda ctx: False)
    res = await T.GoalTool.goal_cancel(t, T.GoalCancelAction(goal_id=g.id, forever=True), None)
    assert res.error and board.get(g.id).status == "ready"
    monkeypatch.setattr(T, "owner_seat_turn", lambda ctx: True)
    res = await T.GoalTool.goal_cancel(t, T.GoalCancelAction(goal_id=g.id, forever=True), None)
    assert not res.error and blocking(board.db_path, user_id="alice", title="gas watch")


# Routines review 2026-10-29 §11: an obsolete ask led every digest for 13 days.

@pytest.mark.asyncio
async def test_goal_cancel_withdraws_an_ask_as_obsolete(tool, monkeypatch):
    t, board = tool
    monkeypatch.setattr(T, "owner_seat_turn", lambda ctx: False)
    ask = board.create_ask(user_id="alice", what="confirm the USDG contract")
    res = await T.GoalTool.goal_cancel(t, T.GoalCancelAction(goal_id=ask.id), None)
    assert not res.error and "no longer needed" in res.extracted_content
    row = board.get(ask.id)
    assert row.status == "obsolete" and row.payload["decision"] == "obsolete"


@pytest.mark.asyncio
async def test_an_ask_that_holds_goals_is_the_owners_to_withdraw(tool, monkeypatch):
    t, board = tool
    g = board.create(user_id="alice", title="pay the round")
    ask = board.create_ask(user_id="alice", what="fund the wallet", blocks_goal_ids=[g.id])
    monkeypatch.setattr(T, "owner_seat_turn", lambda ctx: False)
    res = await T.GoalTool.goal_cancel(t, T.GoalCancelAction(goal_id=ask.id), None)
    assert res.error and board.get(ask.id).status == "open"
    monkeypatch.setattr(T, "owner_seat_turn", lambda ctx: True)
    res = await T.GoalTool.goal_cancel(t, T.GoalCancelAction(goal_id=ask.id), None)
    assert not res.error and board.get(ask.id).status == "obsolete"
