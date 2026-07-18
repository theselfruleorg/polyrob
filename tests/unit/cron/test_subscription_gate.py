"""Task 14 (Phase 3 R5): a watchtower cron job carrying payload.subscription_id
$0-skips once its subscription lapses (suspended/canceled). active/grace still
run (grace keeps delivering while a renewal is chased). Gated
SUBSCRIPTIONS_ENABLED — off is byte-identical (no lookup at all).
"""
from unittest.mock import AsyncMock, patch

import pytest

from cron.runner import make_agent_runner
from tests.unit.cron.test_runner_runloop_delivery import _job


@pytest.fixture(autouse=True)
def _clear_autonomy_marker():
    yield
    from agents.task.goals import autonomy_marker
    autonomy_marker._SESSIONS.clear()


class _Agent:
    def __init__(self):
        self.create_calls = 0
        self.run_calls = 0

    async def create_session(self, *, user_id, request):
        self.create_calls += 1
        return {"id": "sess-1"}

    async def run_session(self, user_id, session_id):
        self.run_calls += 1
        return "done"


@pytest.mark.asyncio
async def test_suspended_subscription_skips_without_invoking_agent(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("modules.x402.subscriptions.subscription_permits_work",
               new=AsyncMock(return_value=False)):
        ok = await runner(_job(payload={"subscription_id": "sub_lapsed"}))
    assert ok is True  # a $0 skip is a success, not a failure
    assert agent.create_calls == 0
    assert agent.run_calls == 0


@pytest.mark.asyncio
async def test_active_subscription_runs_normally(monkeypatch):
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("modules.x402.subscriptions.subscription_permits_work",
               new=AsyncMock(return_value=True)):
        ok = await runner(_job(payload={"subscription_id": "sub_active"}))
    assert ok is True
    assert agent.create_calls == 1
    assert agent.run_calls == 1


@pytest.mark.asyncio
async def test_grace_subscription_runs_normally(monkeypatch):
    """Grace still delivers while a renewal is chased — subscription_permits_work
    itself returns True for grace, so this is really the same code path as
    active; pinned separately since it's an explicit product decision."""
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("modules.x402.subscriptions.subscription_permits_work",
               new=AsyncMock(return_value=True)):
        ok = await runner(_job(payload={"subscription_id": "sub_grace"}))
    assert ok is True
    assert agent.create_calls == 1


@pytest.mark.asyncio
async def test_flag_off_never_consults_subscription_store(monkeypatch):
    """SUBSCRIPTIONS_ENABLED off -> the subscription store is never even
    queried (byte-identical to a job with no subscription_id at all), even
    when payload.subscription_id is present and would otherwise gate."""
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "false")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("modules.x402.subscriptions.subscription_permits_work",
               new=AsyncMock(side_effect=AssertionError("must not be called"))):
        ok = await runner(_job(payload={"subscription_id": "sub_whatever"}))
    assert ok is True
    assert agent.create_calls == 1
    assert agent.run_calls == 1


@pytest.mark.asyncio
async def test_no_subscription_id_unaffected(monkeypatch):
    """A job with no subscription_id at all never touches the gate — the
    common case, byte-identical to before Task 14."""
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("modules.x402.subscriptions.subscription_permits_work",
               new=AsyncMock(side_effect=AssertionError("must not be called"))):
        ok = await runner(_job())
    assert ok is True
    assert agent.create_calls == 1


@pytest.mark.asyncio
async def test_gate_check_error_fails_open_and_runs(monkeypatch):
    """A lookup error must never silently starve a paying customer's job —
    fail-open, run the tick."""
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("SUBSCRIPTIONS_ENABLED", "true")
    agent = _Agent()
    runner = make_agent_runner(agent)
    with patch("modules.x402.subscriptions.subscription_permits_work",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        ok = await runner(_job(payload={"subscription_id": "sub_x"}))
    assert ok is True
    assert agent.create_calls == 1
