"""Stale-completion skip (P5 T7 additional requirement): if the owner cancels/pauses
a goal WHILE it is running, record_success still lands (T2 guards keep the owner's
status — see test_intervention_guards.py), but the dispatcher must not also write an
OUTCOME or fire a self-wake for a run the owner already walked away from.
"""
import asyncio

import pytest

from agents.task.goals.board import GoalBoard, STATUS_CANCELLED
from agents.task.goals.dispatcher import GoalDispatcher


class _CancellingAgent:
    """Cancels the goal (as if the owner intervened) from inside run_session,
    then returns a genuine-looking result with an OUTCOME line."""

    def __init__(self, board):
        self.board = board
        self.woke = []

    async def create_session(self, *, user_id, request):
        # Owner cancels mid-run, before the agent "finishes".
        self.board.cancel(request["goal_id"])
        return {"id": "sess-1"}

    async def run_session(self, user_id, session_id):
        return "did the work\nOUTCOME: wrote project/out.md"

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.woke.append((session_id, text, metadata))
        return True


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


@pytest.mark.asyncio
async def test_stale_completion_skips_outcome_and_self_wake(board, monkeypatch):
    monkeypatch.setenv("GOAL_SELF_WAKE_ENABLED", "true")  # prove the SKIP, not just the gate
    g = board.create(user_id="rob", title="do it")
    board.claim(g.id, "w", ttl_seconds=60)
    agent = _CancellingAgent(board)
    d = GoalDispatcher(board, agent)
    await d._run_goal(board.get(g.id))

    got = board.get(g.id)
    assert got.status == STATUS_CANCELLED  # owner's decision survives
    assert "outcome" not in (got.payload or {})  # no outcome write for a stale completion
    assert agent.woke == []  # no self-wake for a stale completion

    kinds = [e["kind"] for e in board.events(g.id)]
    assert "stale_completion" in kinds
