"""098 AG-09: an owner cancel owns the durable delegation row."""
import asyncio

import pytest

from agents.task.agent.async_delegation import AsyncDelegationRegistry
from agents.task.agent.autonomy_state import AutonomyStateStore


class _Mgr:
    def __init__(self, gate):
        self.gate = gate

    async def run_subtask(self, **kw):
        await self.gate.wait()
        return type("R", (), {"success": True, "finished": True, "output": "ok",
                              "stop_reason": None})()


def _reg(store, mgr, deliver):
    return AsyncDelegationRegistry(manager=mgr, deliver=deliver, session_id="s1",
                                   user_id="u1", store=store)


@pytest.fixture
def store(tmp_path):
    return AutonomyStateStore(str(tmp_path / "autonomy_state.db"))


@pytest.mark.asyncio
async def test_cancel_before_start_writes_cancelled_row(store):
    async def deliver(rec, block):
        pass
    reg = _reg(store, _Mgr(asyncio.Event()), deliver)
    h = await reg.dispatch(goal="g", parent_agent_id="a")
    assert reg.cancel_all("owner pause") == 1      # before the task's first step
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    row = store.get("s1", h["delegation_id"])
    assert row["status"] == "cancelled"
    assert store.list_running() == []


@pytest.mark.asyncio
async def test_cancel_during_delivery_is_not_redelivered_after_boot(store):
    gate, parked = asyncio.Event(), asyncio.Event()

    async def deliver(rec, block):
        parked.set()
        await asyncio.Event().wait()             # parking hangs

    reg = _reg(store, _Mgr(gate), deliver)
    h = await reg.dispatch(goal="g", parent_agent_id="a")
    gate.set()
    await asyncio.wait_for(parked.wait(), 2)
    reg.cancel_all("owner pause")
    await asyncio.sleep(0)
    assert store.get("s1", h["delegation_id"])["status"] == "completed"
    assert store.list_completed_undelivered() == []


@pytest.mark.asyncio
async def test_cancel_all_cas_does_not_clobber_completed(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0009",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    store.record_terminal("s1", "deleg_0009", status="completed", completed_at=2.0,
                          result_text="r")
    reg = _reg(store, _Mgr(asyncio.Event()), None)
    reg._durable_cancel("deleg_0009")
    row = store.get("s1", "deleg_0009")
    assert row["status"] == "completed" and row["delivered_at"] is not None
