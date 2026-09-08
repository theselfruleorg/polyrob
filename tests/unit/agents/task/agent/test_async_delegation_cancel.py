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
