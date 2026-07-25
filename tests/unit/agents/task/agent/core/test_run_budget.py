"""Truth-table for the run-budget gate policy (T1.1)."""
from types import SimpleNamespace

import pytest

import agents.task.agent.core.run_budget as rb
from agents.task.agent.core.run_budget import RUN_BUDGET_MARKER, check_run_budget


def _agent(*, sub=False, tracker=SimpleNamespace(db=None), user_id="u1"):
    return SimpleNamespace(
        _is_sub_agent=sub,
        usage_tracker=tracker,
        user_id=user_id,
        session_id="sess-1",
    )


def _fake_rollup(spent):
    calls = []
    async def rollup(user_id, session_id=None, since=None, *, db=None):
        calls.append((user_id, session_id))
        return {"user_id": user_id, "session_id": session_id,
                "api_cost_usd": spent, "credits": 0, "calls": 3}
    rollup.calls = calls
    return rollup


@pytest.mark.asyncio
async def test_flag_off_never_queries(monkeypatch):
    monkeypatch.delenv("RUN_BUDGET_USD", raising=False)
    fake = _fake_rollup(99.0)
    monkeypatch.setattr(rb, "usage_rollup", fake)
    assert await check_run_budget(_agent()) is None
    assert fake.calls == []  # zero overhead when disabled


@pytest.mark.asyncio
async def test_over_budget_returns_marker_message(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", _fake_rollup(0.25))
    msg = await check_run_budget(_agent())
    assert msg is not None and msg.startswith(RUN_BUDGET_MARKER + ":")
    assert "0.25" in msg and "0.10" in msg


@pytest.mark.asyncio
async def test_under_budget_is_none(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", _fake_rollup(0.05))
    assert await check_run_budget(_agent()) is None


@pytest.mark.asyncio
async def test_exactly_at_budget_halts(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", _fake_rollup(0.10))
    assert (await check_run_budget(_agent())) is not None  # >= is the contract


@pytest.mark.asyncio
async def test_sub_agent_is_ungated(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    fake = _fake_rollup(9.9)
    monkeypatch.setattr(rb, "usage_rollup", fake)
    assert await check_run_budget(_agent(sub=True)) is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_no_tracker_or_no_user_skips(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    fake = _fake_rollup(9.9)
    monkeypatch.setattr(rb, "usage_rollup", fake)
    assert await check_run_budget(_agent(tracker=None)) is None
    assert await check_run_budget(_agent(user_id="")) is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_rollup_error_fails_open(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    async def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(rb, "usage_rollup", boom)
    assert await check_run_budget(_agent()) is None  # never kills a healthy run


@pytest.mark.asyncio
async def test_missing_credits_module_fails_open(monkeypatch):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "usage_rollup", None)  # core-only install shape
    assert await check_run_budget(_agent()) is None


# --- Final-review fix (T1.1): one-time inert-gate warning -------------------


@pytest.mark.asyncio
async def test_inert_gate_warns_once_across_two_calls(monkeypatch, caplog):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "_warned_inert", False)
    fake = _fake_rollup(9.9)
    monkeypatch.setattr(rb, "usage_rollup", fake)
    with caplog.at_level("WARNING", logger="agents.task.agent.core.run_budget"):
        assert await check_run_budget(_agent(tracker=None)) is None
        assert await check_run_budget(_agent(tracker=None)) is None
    warnings = [r for r in caplog.records if "cannot operate" in r.message]
    assert len(warnings) == 1
    assert fake.calls == []  # still never queries — precondition failed


@pytest.mark.asyncio
async def test_flag_off_never_warns(monkeypatch, caplog):
    monkeypatch.delenv("RUN_BUDGET_USD", raising=False)
    monkeypatch.setattr(rb, "_warned_inert", False)
    with caplog.at_level("WARNING", logger="agents.task.agent.core.run_budget"):
        assert await check_run_budget(_agent(tracker=None)) is None
    assert not any("cannot operate" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_sub_agent_skip_never_warns(monkeypatch, caplog):
    monkeypatch.setenv("RUN_BUDGET_USD", "0.10")
    monkeypatch.setattr(rb, "_warned_inert", False)
    with caplog.at_level("WARNING", logger="agents.task.agent.core.run_budget"):
        assert await check_run_budget(_agent(sub=True, tracker=None)) is None
    assert not any("cannot operate" in r.message for r in caplog.records)
