"""`TaskAgent._resume_locks` drops a session's entry once nobody holds or waits on it.

Before: one asyncio.Lock per dead session id, kept forever (unbounded growth).
"""
import asyncio

import pytest

from agents.task.conversation_resume import ConversationResumeMixin


class _Host(ConversationResumeMixin):
    pass


@pytest.mark.asyncio
async def test_entry_is_dropped_after_release():
    host = _Host()
    async with host._resume_lock_scope("dead-1"):
        assert "dead-1" in host._resume_locks
    assert host._resume_locks == {}


@pytest.mark.asyncio
async def test_single_flight_holds_while_a_waiter_is_queued():
    host = _Host()
    order = []
    release = asyncio.Event()

    async def first():
        async with host._resume_lock_scope("dead-1"):
            order.append("first-in")
            await release.wait()
            order.append("first-out")

    async def second():
        async with host._resume_lock_scope("dead-1"):
            order.append("second-in")

    t1 = asyncio.create_task(first())
    await asyncio.sleep(0)
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert "dead-1" in host._resume_locks
    release.set()
    await asyncio.gather(t1, t2)

    assert order == ["first-in", "first-out", "second-in"]
    assert host._resume_locks == {}
