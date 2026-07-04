"""a3: _evict_session must never reap a session whose execution lock is held (a live
run loop), and must pop the per-session _recreate_locks entry when it does evict so the
lock map can't leak unboundedly."""
import asyncio
from types import SimpleNamespace

import pytest


def _agent():
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)  # bypass __init__
    a._session_execution_locks = {}
    a._recreate_locks = {}
    a._session_last_activity = {}
    a.telemetry = None
    a.max_sessions_in_memory = 100
    return a


class _Orch:
    def __init__(self):
        self.cleaned = False
        self.agents = {}
        self.user_id = "u1"
    async def cleanup(self, **kw):
        self.cleaned = True


@pytest.mark.asyncio
async def test_evict_skips_running_session_and_keeps_lock():
    a = _agent()
    orch = _Orch()
    removed = {"n": 0}
    a._registry = SimpleNamespace(
        get=lambda sid: orch,
        remove=lambda sid: removed.__setitem__("n", removed["n"] + 1),
        count=lambda: 1,
    )
    # Simulate an in-flight run: the execution lock is held.
    lock = asyncio.Lock()
    await lock.acquire()
    a._session_execution_locks["s1"] = lock
    a._recreate_locks["s1"] = asyncio.Lock()
    try:
        await a._evict_session("s1", reason="ttl")
    finally:
        lock.release()
    assert orch.cleaned is False          # the live session was NOT torn down
    assert removed["n"] == 0              # nor removed from the registry
    assert "s1" in a._session_execution_locks  # its lock was not popped mid-run


@pytest.mark.asyncio
async def test_evict_idle_session_pops_recreate_lock():
    a = _agent()
    orch = _Orch()
    removed = {"ids": []}
    a._registry = SimpleNamespace(
        get=lambda sid: orch,
        remove=lambda sid: removed["ids"].append(sid),
        count=lambda: 1,
    )
    a._session_execution_locks["s1"] = asyncio.Lock()  # not held
    a._recreate_locks["s1"] = asyncio.Lock()
    await a._evict_session("s1", reason="ttl")
    assert orch.cleaned is True
    assert removed["ids"] == ["s1"]
    assert "s1" not in a._session_execution_locks  # execution lock popped
    assert "s1" not in a._recreate_locks           # a3: recreate lock popped (no leak)
