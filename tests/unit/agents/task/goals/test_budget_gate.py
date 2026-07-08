"""Budget-aware dispatch — over budget raises an owner-visible ask; under runs."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agents.task.goals.board import GoalBoard
from agents.task.goals.dispatcher import GoalDispatcher


class _FakeAgent:
    def __init__(self, final="result"):
        self.final = final
        self.ran = []

    async def create_session(self, *, user_id, request):
        return {"id": f"sess-{user_id}"}

    async def run_session(self, user_id, session_id):
        self.ran.append((user_id, session_id))
        return self.final

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        return True

    def get_orchestrator(self, session_id):
        return None


@pytest.fixture
def board(tmp_path):
    return GoalBoard(str(tmp_path / "goals.db"))


@pytest.mark.asyncio
async def test_over_budget_goal_escalates_ask_not_claimed(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "true")
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "1")
    g = board.create(user_id="u1", title="expensive", body="do work")
    disp = GoalDispatcher(board, _FakeAgent())
    with patch("modules.credits.unified_ledger.build_ledger",
               new=AsyncMock(return_value={"total_spend_usd": 5.0})):
        await disp.dispatch_once()
        await asyncio.sleep(0.05)
    assert board.get(g.id).status == "ready"          # not claimed
    assert board.asks(user_id="u1", status="open")     # ask raised


@pytest.mark.asyncio
async def test_under_budget_goal_runs(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "true")
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "100")
    g = board.create(user_id="u1", title="cheap", body="do work")
    agent = _FakeAgent(final="done")
    disp = GoalDispatcher(board, agent)
    with patch("modules.credits.unified_ledger.build_ledger",
               new=AsyncMock(return_value={"total_spend_usd": 5.0})):
        await disp.dispatch_once()
        await asyncio.sleep(0.05)
    assert board.get(g.id).status in ("running", "done")
    assert not board.asks(user_id="u1", status="open")


@pytest.mark.asyncio
async def test_budget_zero_disables_gate(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "true")
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "0")  # <=0 => never over budget
    g = board.create(user_id="u1", title="run", body="do work")
    disp = GoalDispatcher(board, _FakeAgent(final="done"))
    with patch("modules.credits.unified_ledger.build_ledger",
               new=AsyncMock(return_value={"total_spend_usd": 999.0})):
        await disp.dispatch_once()
        await asyncio.sleep(0.05)
    assert board.get(g.id).status in ("running", "done")


@pytest.mark.asyncio
async def test_gate_off_ignores_budget(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "false")
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "1")
    g = board.create(user_id="u1", title="run", body="do work")
    disp = GoalDispatcher(board, _FakeAgent(final="done"))
    # build_ledger must not even be consulted when the gate is off
    with patch("modules.credits.unified_ledger.build_ledger",
               new=AsyncMock(side_effect=AssertionError("should not be called"))):
        await disp.dispatch_once()
        await asyncio.sleep(0.05)
    assert board.get(g.id).status in ("running", "done")


@pytest.mark.asyncio
async def test_ledger_error_fails_open_and_runs(board, monkeypatch):
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "true")
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "1")
    g = board.create(user_id="u1", title="run", body="do work")
    disp = GoalDispatcher(board, _FakeAgent(final="done"))
    with patch("modules.credits.unified_ledger.build_ledger",
               new=AsyncMock(side_effect=RuntimeError("ledger down"))):
        await disp.dispatch_once()
        await asyncio.sleep(0.05)
    assert board.get(g.id).status in ("running", "done")  # fail-open: not held


@pytest.mark.asyncio
async def test_over_budget_push_fires_once_per_episode(board, monkeypatch):
    # The durable ask dedup-refreshes every tick, but the owner PUSH must fire ONCE
    # per over-budget episode, not every tick per held goal (spam guard).
    monkeypatch.setenv("GOALS_ENABLED", "true")
    monkeypatch.setenv("BUDGET_AWARE_AUTONOMY", "true")
    monkeypatch.setenv("AUTONOMY_BUDGET_USD", "1")
    pushes = []

    class _Container:
        pass

    agent = _FakeAgent()
    agent.container = _Container()

    async def _fake_push(container, text):
        pushes.append(text)
        return True

    monkeypatch.setattr("core.self_evolution.push_owner_message", _fake_push)
    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger",
                        AsyncMock(return_value={"total_spend_usd": 5.0}))
    disp = GoalDispatcher(board, agent)
    g1 = board.create(user_id="u1", title="g1", body="x")
    g2 = board.create(user_id="u1", title="g2", body="x")

    # simulate three dispatch ticks, each evaluating both held goals
    for _ in range(3):
        await disp._over_budget(g1)
        await disp._over_budget(g2)
    assert len(pushes) == 1  # exactly one push for the whole episode / tenant

    # tenant recovers under budget -> latch clears -> a new episode pushes again
    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger",
                        AsyncMock(return_value={"total_spend_usd": 0.0}))
    await disp._over_budget(g1)  # under budget now, clears latch
    monkeypatch.setattr("modules.credits.unified_ledger.build_ledger",
                        AsyncMock(return_value={"total_spend_usd": 5.0}))
    await disp._over_budget(g1)  # over again -> pushes
    assert len(pushes) == 2
