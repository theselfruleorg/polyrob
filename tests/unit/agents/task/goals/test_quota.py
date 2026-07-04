import asyncio

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


class _NullAgent:
    async def create_session(self, user_id, request):
        return {"id": "s1"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "g.db"))


def test_count_started_since(board):
    g1 = board.create(user_id="rob", title="goal one distinct")
    board.claim(g1.id, "w", ttl_seconds=60)   # sets started_at
    board.create(user_id="rob", title="never started goal")
    assert board.count_started_since(86400) == 1


def test_quota_blocks_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "1")
    monkeypatch.setenv("GOAL_DISPATCH_INTERVAL_SEC", "60")
    board = GoalBoard(str(tmp_path / "g.db"))
    g1 = board.create(user_id="rob", title="goal one distinct")
    board.claim(g1.id, "w", ttl_seconds=60)
    board.record_success(g1.id, result="ok")  # 1 started in last 24h
    board.create(user_id="rob", title="another different goal")
    d = GoalDispatcher(board, _NullAgent())
    dispatched = asyncio.run(d.dispatch_once())
    assert dispatched == 0  # quota=1 already used


def test_quota_zero_disables_rail(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "0")
    board = GoalBoard(str(tmp_path / "g.db"))
    board.create(user_id="rob", title="a runnable goal")
    d = GoalDispatcher(board, _NullAgent())
    dispatched = asyncio.run(d.dispatch_once())
    assert dispatched == 1
