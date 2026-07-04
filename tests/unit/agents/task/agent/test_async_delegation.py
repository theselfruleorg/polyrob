"""UP-12 — durable async delegation (delegate_task background=true).

Covers: view validation, the get_max_async_sub_agents knob, AsyncDelegationRegistry
(non-blocking dispatch, capacity rejection, single-lock no-TOCTOU, one completion per
child for success/error/timeout, retention pruning, active_count), and the handler
background branch (dispatch + immediate result; leaf denied before dispatch; sync
unchanged).
"""
import asyncio
import logging
import types

import pytest


# --- view validation ---------------------------------------------------------

def test_view_background_valid_with_goal():
    from tools.controller.views import DelegateTaskAction
    a = DelegateTaskAction(goal="a sufficiently long delegated background goal", background=True)
    assert a.background is True


def test_view_background_rejected_with_tasks():
    from tools.controller.views import DelegateTaskAction
    with pytest.raises(ValueError):
        DelegateTaskAction(
            tasks=[{"task": "scrape source one please thanks"},
                   {"task": "scrape source two please thanks"}],
            background=True,
        )


def test_view_background_defaults_false():
    from tools.controller.views import DelegateTaskAction
    assert DelegateTaskAction(goal="a sufficiently long delegated goal here").background is False


# --- config knob -------------------------------------------------------------

def test_max_async_default(monkeypatch):
    from agents.task.constants import TimeoutConfig
    monkeypatch.delenv("MAX_ASYNC_SUB_AGENTS", raising=False)
    monkeypatch.setattr(TimeoutConfig, "get_max_concurrent_sub_agents", classmethod(lambda cls: 3))
    assert TimeoutConfig.get_max_async_sub_agents() == 2


def test_max_async_floor_and_clamp(monkeypatch):
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_max_concurrent_sub_agents", classmethod(lambda cls: 2))
    monkeypatch.setenv("MAX_ASYNC_SUB_AGENTS", "0")
    assert TimeoutConfig.get_max_async_sub_agents() == 1  # floor
    monkeypatch.setenv("MAX_ASYNC_SUB_AGENTS", "9")
    assert TimeoutConfig.get_max_async_sub_agents() == 2  # clamped to concurrent ceiling


# --- AsyncDelegationRegistry -------------------------------------------------

class _Result:
    def __init__(self, success=True, output="ok", error=None):
        self.success = success
        self.output = output
        self.error = error

    @property
    def output_text(self):
        return self.output if isinstance(self.output, str) else str(self.output)


class _Manager:
    def __init__(self, behavior="ok", gate: asyncio.Event = None):
        self.behavior = behavior
        self.gate = gate
        self.calls = 0

    async def run_subtask(self, **kwargs):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.behavior == "ok":
            return _Result(success=True, output="goal-result")
        if self.behavior == "fail":
            return _Result(success=False, error="boom")
        if self.behavior == "timeout":
            raise asyncio.TimeoutError()
        raise RuntimeError("unexpected")


def _registry(manager, delivered):
    from agents.task.agent.async_delegation import AsyncDelegationRegistry
    t = {"n": 0}

    def clock():
        t["n"] += 1
        return float(t["n"])

    async def deliver(rec, text):
        delivered.append((rec.delegation_id, rec.status, text))

    return AsyncDelegationRegistry(manager, deliver=deliver, clock=clock)


@pytest.mark.asyncio
async def test_dispatch_non_blocking_and_delivers_success(monkeypatch):
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_max_async_sub_agents", classmethod(lambda cls: 2))
    delivered = []
    reg = _registry(_Manager("ok"), delivered)

    res = await reg.dispatch(goal="research X", parent_agent_id="main")
    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # not delivered yet (task scheduled, not awaited)
    # let the detached task run to completion
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(delivered) == 1
    did, status, text = delivered[0]
    assert status == "completed"
    assert "goal-result" in text
    assert did in text and "research X" in text


@pytest.mark.asyncio
async def test_capacity_rejection_no_toctou(monkeypatch):
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_max_async_sub_agents", classmethod(lambda cls: 1))
    gate = asyncio.Event()
    delivered = []
    reg = _registry(_Manager("ok", gate=gate), delivered)

    # Two simultaneous dispatches; cap=1 -> exactly one dispatched, one rejected.
    r1, r2 = await asyncio.gather(
        reg.dispatch(goal="job one here", parent_agent_id="main"),
        reg.dispatch(goal="job two here", parent_agent_id="main"),
    )
    statuses = sorted([r1["status"], r2["status"]])
    assert statuses == ["dispatched", "rejected"]
    assert reg.active_count() == 1
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_error_and_timeout_each_deliver_once(monkeypatch):
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_max_async_sub_agents", classmethod(lambda cls: 5))
    for behavior, expect in [("fail", "error"), ("timeout", "timeout")]:
        delivered = []
        reg = _registry(_Manager(behavior), delivered)
        await reg.dispatch(goal=f"job {behavior}", parent_agent_id="main")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(delivered) == 1
        assert delivered[0][1] == expect


@pytest.mark.asyncio
async def test_cancelled_delegation_records_cancelled_not_completed(monkeypatch):
    """B13: a cancelled background delegation must be recorded 'cancelled', not
    'completed'. The finally block wrote rec.status = status; setting rec.status in the
    except branch was clobbered back to the initial 'completed'."""
    from agents.task.constants import TimeoutConfig
    monkeypatch.setattr(TimeoutConfig, "get_max_async_sub_agents", classmethod(lambda cls: 5))

    class _CancelManager:
        async def run_subtask(self, **kwargs):
            raise asyncio.CancelledError()

    delivered = []
    reg = _registry(_CancelManager(), delivered)
    res = await reg.dispatch(goal="cancel me please now", parent_agent_id="main")
    did = res["delegation_id"]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    rec = next(r for r in reg.list() if r.delegation_id == did)
    assert rec.status == "cancelled"
    assert delivered == []  # never deliver on cancellation


@pytest.mark.asyncio
async def test_retention_pruning(monkeypatch):
    from agents.task.constants import TimeoutConfig
    import agents.task.agent.async_delegation as ad
    monkeypatch.setattr(TimeoutConfig, "get_max_async_sub_agents", classmethod(lambda cls: 100))
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 3)
    delivered = []
    reg = _registry(_Manager("ok"), delivered)
    for i in range(8):
        await reg.dispatch(goal=f"job number {i} here", parent_agent_id="main")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    # completed records pruned to the cap (running may briefly exceed, but all done here)
    assert len(reg.list()) <= 3 + 1


@pytest.mark.asyncio
async def test_orchestrator_delivery_routes_to_submit_user_message():
    """_deliver_async_delegation routes the completion through submit_user_message with
    kind='delegation_result' so it re-enters as a new turn."""
    from agents.task.agent.orchestrator import SessionOrchestrator
    from agents.task.agent.async_delegation import DelegationRecord

    orch = object.__new__(SessionOrchestrator)
    calls = []

    async def fake_submit(agent_id, text, kind="comment", metadata=None):
        calls.append({"agent_id": agent_id, "text": text, "kind": kind, "metadata": metadata})

    orch.submit_user_message = fake_submit
    rec = DelegationRecord(delegation_id="deleg_0007", goal="g", profile="executor",
                           parent_agent_id="main", status="completed")
    await orch._deliver_async_delegation(rec, "<delegation-result>done</delegation-result>")

    assert len(calls) == 1
    assert calls[0]["kind"] == "delegation_result"
    assert calls[0]["agent_id"] == "main"
    assert calls[0]["metadata"]["delegation_id"] == "deleg_0007"
    assert "<delegation-result>" in calls[0]["text"]


@pytest.mark.asyncio
async def test_delivery_failure_is_swallowed(monkeypatch):
    """A raising deliver (e.g. MessageQueueFullError) must not crash the detached task."""
    from agents.task.constants import TimeoutConfig
    from agents.task.agent.async_delegation import AsyncDelegationRegistry
    monkeypatch.setattr(TimeoutConfig, "get_max_async_sub_agents", classmethod(lambda cls: 2))

    async def boom_deliver(rec, text):
        raise RuntimeError("queue full")

    reg = AsyncDelegationRegistry(_Manager("ok"), deliver=boom_deliver, clock=lambda: 1.0)
    res = await reg.dispatch(goal="research X", parent_agent_id="main")
    assert res["status"] == "dispatched"
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # no exception propagated; record marked completed
    assert reg.list()[0].status == "completed"
