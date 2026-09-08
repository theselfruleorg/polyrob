"""``goal_list`` must show the agent its LIVE board, newest first (2026-08-29 C7).

Before: ``board.list(user_id, status=None)`` — ``priority DESC, created_at ASC
LIMIT 100``. On a 409-row prod board that window was the oldest 100 rows and held
zero manifest stream legs (priority 2/3 sort after every priority-5 row), so the
agent's own listing said it had no trading goals while ten clean cycles had run.
"""
import asyncio
from types import SimpleNamespace

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalListAction, GoalTool


def _tool(tmp_path):
    t = GoalTool.__new__(GoalTool)
    t._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return t


def _ctx(session_id="owner-sess", user_id="rob"):
    return SimpleNamespace(session_id=session_id, parent_session_id=None, user_id=user_id)


def _done(board, title, priority=5):
    g = board.create(user_id="rob", title=title, priority=priority, force=True)
    board.claim(g.id, "w", ttl_seconds=60)
    board.record_success(g.id, result="ok")
    return g


def run(coro):
    return asyncio.run(coro)


def test_goal_list_defaults_to_live_goals_newest_first(tmp_path):
    t = _tool(tmp_path)
    b = t._goal_board
    for i in range(120):
        _done(b, f"old finished work {i}")
    older_live = b.create(user_id="rob", title="older live goal", priority=5, force=True)
    stream_leg = b.create(user_id="rob", title="Treasury: manage open positions",
                          priority=2, force=True, payload={"stream": "treasury-trading"})

    r = run(t.goal_list(GoalListAction(), _ctx()))

    text = r.extracted_content
    assert "Treasury: manage open positions" in text
    assert text.index(stream_leg.id) < text.index(older_live.id)  # newest first
    assert "old finished work" not in text                        # done is not live


def test_goal_list_reports_totals_when_nothing_is_live(tmp_path):
    t = _tool(tmp_path)
    for i in range(3):
        _done(t._goal_board, f"finished {i}")
    r = run(t.goal_list(GoalListAction(), _ctx()))
    assert "no live goals" in r.extracted_content.lower()
    assert "3 done" in r.extracted_content


def test_goal_list_explicit_status_is_newest_first_and_capped(tmp_path):
    t = _tool(tmp_path)
    b = t._goal_board
    first = _done(b, "first finished")
    last = _done(b, "last finished")
    r = run(t.goal_list(GoalListAction(status="done", limit=1), _ctx()))
    assert last.id in r.extracted_content and first.id not in r.extracted_content


def test_goal_list_is_tenant_scoped(tmp_path):
    t = _tool(tmp_path)
    t._goal_board.create(user_id="other", title="not yours", force=True)
    r = run(t.goal_list(GoalListAction(), _ctx()))
    assert "not yours" not in r.extracted_content
