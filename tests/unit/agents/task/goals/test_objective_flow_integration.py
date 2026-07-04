"""End-to-end (no LLM): objective -> planner prompt -> goal_create w/ dedup ->
dispatch -> outcome recorded -> next planner prompt includes the outcome."""
import asyncio

import pytest

from agents.task.goals.board import DuplicateGoalError, GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher
from agents.task.goals.planner import build_planner_prompt


class _OutcomeAgent:
    async def create_session(self, user_id, request):
        return {"id": "s-int-1"}

    async def run_session(self, user_id, session_id):
        return "Wrote the draft.\nOUTCOME: project/drafts/pricing.md"


def test_full_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "6")
    board = GoalBoard(str(tmp_path / "g.db"))
    o = board.create_objective(user_id="rob", title="Grow the substack")

    # planner prompt names the objective
    p1 = build_planner_prompt(board, "rob", None)
    assert o.id in p1

    # agent creates a goal against it; a near-dup is mechanically rejected
    g = board.create(user_id="rob", title="Draft pricing post for substack",
                     parent_id=o.id, payload={"acceptance": "a draft file"})
    with pytest.raises(DuplicateGoalError):
        board.create(user_id="rob", title="Draft pricing post for the substack")

    d = GoalDispatcher(board, _OutcomeAgent())

    # Dispatch AND drain on the SAME event loop — the goal runs fire-and-forget as a
    # task, so a second asyncio.run() would close the loop and orphan/cancel it before
    # it completes (in production the dispatcher runs on a persistent ticker loop).
    async def _cycle():
        assert await d.dispatch_once() == 1
        for _ in range(200):
            if board.get(g.id).status == "done":
                return
            await asyncio.sleep(0.01)
    asyncio.run(_cycle())

    done = board.get(g.id)
    assert done.status == "done"
    assert done.payload["outcome"] == "project/drafts/pricing.md"

    # the NEXT planner prompt carries the outcome forward
    p2 = build_planner_prompt(board, "rob", None)
    assert "project/drafts/pricing.md" in p2
