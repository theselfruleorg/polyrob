"""`goal_ask` — a rail that needs an OWNER DECISION raises a durable board ask.

2026-09-20 (intel 09:57Z): the 6-hourly PNL buyback skipped 12 h on a gate-1 A/B
question Rob had asked ONCE in chat and never re-raised — nothing agent-callable
reached `GoalBoard.create_ask`, so the question lived in a note to himself and
vanished from every owner seat. The store already dedups OPEN asks (a matching
one is refreshed), so re-asking is safe.
"""
import asyncio
from types import SimpleNamespace

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalAskAction, GoalTool


def _tool(tmp_path):
    t = GoalTool.__new__(GoalTool)
    t._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return t


def _ctx(session_id="rail-sess", user_id="rob", **kw):
    return SimpleNamespace(session_id=session_id, parent_session_id=None, user_id=user_id, **kw)


def test_ask_lands_on_the_board_open_with_rail_provenance(tmp_path):
    t = _tool(tmp_path)
    r = asyncio.run(t.goal_ask(GoalAskAction(
        what="Gate 1 reference — 24h high (A) or most recent 2h reading (B)?",
        why="03:33 and 09:34 cycles skipped on gate 1 alone"), _ctx()))
    assert not r.error and "ask" in r.extracted_content.lower()
    asks = t._goal_board.asks(user_id="rob", status="open")
    assert len(asks) == 1
    a = asks[0]
    assert a.payload.get("origin") == "agent" and a.payload.get("session_id") == "rail-sess"
    assert a.id in r.extracted_content


def test_a_repeat_ask_is_refreshed_not_duplicated_and_the_result_says_so(tmp_path):
    t = _tool(tmp_path)
    a = GoalAskAction(what="Gate 1 reference — 24h high (A) or most recent 2h reading (B)?", why="skip")
    r1 = asyncio.run(t.goal_ask(a, _ctx()))
    r2 = asyncio.run(t.goal_ask(a, _ctx(session_id="rail-sess-2")))
    assert len(t._goal_board.asks(user_id="rob", status="open")) == 1
    assert "already open" in r2.extracted_content.lower()
    assert "no need to re-ask" in r2.extracted_content.lower() or "dedup" in r2.extracted_content.lower()
    assert r1.extracted_content != r2.extracted_content


def test_a_leaf_or_sub_agent_cannot_raise_an_owner_ask(tmp_path):
    t = _tool(tmp_path)
    a = GoalAskAction(what="please decide X")
    r = asyncio.run(t.goal_ask(a, _ctx(is_sub_agent=True)))
    assert r.error and "leaf" in r.error.lower() or "sub-agent" in (r.error or "").lower()
    r2 = asyncio.run(t.goal_ask(a, _ctx(role="leaf")))
    assert r2.error
    assert t._goal_board.asks(user_id="rob", status="open") == []


def test_blocks_goal_ids_are_recorded(tmp_path):
    t = _tool(tmp_path)
    g = t._goal_board.create(user_id="rob", title="buy PNL tranche 15", force=True)
    r = asyncio.run(t.goal_ask(GoalAskAction(what="decide X", blocks_goal_ids=[g.id]), _ctx()))
    assert not r.error
    a = t._goal_board.asks(user_id="rob", status="open")[0]
    assert g.id in (a.payload.get("blocks_goal_ids") or [])
