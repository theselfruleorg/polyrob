"""031 T6: an owner pause cancels every running background delegation."""
import asyncio

import pytest

from agents.task.agent.async_delegation import AsyncDelegationRegistry, DelegationRecord


@pytest.mark.asyncio
async def test_cancel_all_cancels_running_records():
    async def _deliver(rec, text):
        return None

    reg = AsyncDelegationRegistry(manager=object(), deliver=_deliver)
    ev = asyncio.Event()

    async def body():
        ev.set()
        await asyncio.sleep(30)

    t = asyncio.create_task(body())
    reg._records["deleg_0001"] = DelegationRecord(delegation_id="deleg_0001", goal="g",
                                                  profile="executor", status="running", task=t)
    reg._records["deleg_0002"] = DelegationRecord(delegation_id="deleg_0002", goal="g2",
                                                  profile="executor", status="completed")
    await ev.wait()
    n = reg.cancel_all("owner pause")
    await asyncio.sleep(0)
    assert n == 1 and t.cancelled()
    assert reg._records["deleg_0001"].status == "cancelled"
    assert reg._records["deleg_0002"].status == "completed"
    assert reg.cancel_all("again") == 0


class _Store:
    """Records the durable writes the cancel makes."""

    def __init__(self, running: bool = True):
        self.running = running
        self.terminal, self.delivered = [], []

    def record_terminal(self, session_id, delegation_id, **kw):
        self.terminal.append(delegation_id)
        return self.running

    def mark_delivered(self, session_id, delegation_id, at):
        self.delivered.append(delegation_id)


async def _parked_task():
    ev = asyncio.Event()

    async def body():
        ev.set()
        await asyncio.sleep(30)

    t = asyncio.create_task(body())
    await ev.wait()
    return t


@pytest.mark.asyncio
async def test_cancel_one_running_id_moves_its_durable_row():
    """120 W2: the per-id cancel is the one cancel; cancel_all loops over it."""
    async def _deliver(rec, text):
        return None

    store = _Store()
    reg = AsyncDelegationRegistry(manager=object(), deliver=_deliver, store=store,
                                  session_id="s1")
    t1, t2 = await _parked_task(), await _parked_task()
    reg._records["deleg_0001"] = DelegationRecord(delegation_id="deleg_0001", goal="g",
                                                  profile="executor", task=t1)
    reg._records["deleg_0002"] = DelegationRecord(delegation_id="deleg_0002", goal="g2",
                                                  profile="executor", task=t2)
    assert reg.cancel("deleg_0001", "agent stop") == "cancelled"
    await asyncio.sleep(0)
    assert t1.cancelled() and not t2.cancelled()
    assert reg._records["deleg_0001"].status == "cancelled"
    assert store.terminal == ["deleg_0001"]
    assert reg.cancel("deleg_0001", "again") == ""
    assert reg.cancel("deleg_9999", "unknown") == ""
    t2.cancel()


@pytest.mark.asyncio
async def test_cancel_of_a_finished_helper_parking_its_result_is_not_redelivered():
    async def _deliver(rec, text):
        return None

    store = _Store()
    reg = AsyncDelegationRegistry(manager=object(), deliver=_deliver, store=store,
                                  session_id="s1")
    t = await _parked_task()
    reg._records["deleg_0001"] = DelegationRecord(delegation_id="deleg_0001", goal="g",
                                                  profile="executor", status="completed",
                                                  task=t)
    assert reg.cancel("deleg_0001", "agent stop") == "stopped"
    await asyncio.sleep(0)
    assert t.cancelled() and store.delivered == ["deleg_0001"]
    assert reg._records["deleg_0001"].status == "completed"
