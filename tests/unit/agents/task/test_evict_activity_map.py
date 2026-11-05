"""102 N-7: an id with no orchestrator must leave the activity map at eviction.

The pop sat inside ``if orchestrator``, after the await. A stale id then re-entered
eviction on every sweep and took an LRU slot, so a real session could stay over the limit.
"""
import asyncio
from types import SimpleNamespace

import pytest


class _Registry:
    def __init__(self, **orchs):
        self.d = dict(orchs)

    def get(self, sid):
        return self.d.get(sid)

    def remove(self, sid):
        return self.d.pop(sid, None)

    def count(self):
        return len(self.d)


def _agent(**orchs):
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)
    a._session_execution_locks = {}
    a._recreate_locks = {}
    a._session_last_activity = {}
    a.telemetry = None
    a.session_manager = None
    a.max_sessions_in_memory = 100
    a.session_ttl_seconds = 10_000
    a._registry = _Registry(**orchs)
    return a


class _Orch:
    def __init__(self, boom=False):
        self.agents, self.user_id, self.boom = {}, "u1", boom
        self.cleaned = False

    async def cleanup(self, **kw):
        self.cleaned = True
        if self.boom:
            raise RuntimeError("cleanup failed")


@pytest.mark.asyncio
async def test_a_stale_id_leaves_the_activity_map():
    a = _agent()
    a._session_last_activity["gone"] = 1.0
    await a._evict_session("gone", reason="ttl")
    assert "gone" not in a._session_last_activity


@pytest.mark.asyncio
async def test_a_failed_cleanup_still_leaves_the_activity_map():
    a = _agent(s1=_Orch(boom=True))
    a._session_last_activity["s1"] = 1.0
    await a._evict_session("s1", reason="ttl")
    assert a._registry.get("s1") is None
    assert "s1" not in a._session_last_activity


@pytest.mark.asyncio
async def test_a_session_with_a_live_run_keeps_its_activity():
    a = _agent(s1=_Orch())
    a._session_last_activity["s1"] = 1.0
    a._session_execution_locks["s1"] = lock = asyncio.Lock()
    await lock.acquire()
    await a._evict_session("s1", reason="ttl")
    assert a._session_last_activity["s1"] == 1.0
    assert a._registry.get("s1") is not None


@pytest.mark.asyncio
async def test_a_stale_id_does_not_take_an_lru_slot():
    now = 1_000_000.0
    real = _Orch()
    a = _agent(old=real, new=_Orch())
    a.max_sessions_in_memory = 1
    a.session_ttl_seconds = 10 ** 10
    a._session_last_activity.update({"stale": now - 2, "old": now - 1, "new": now})
    await a._cleanup_old_sessions()
    assert real.cleaned, "the oldest RESIDENT session is the one evicted"
    assert a._registry.count() == 1


def test_remove_orchestrator_clears_the_activity():
    a = _agent(s1=SimpleNamespace())
    a._session_last_activity["s1"] = 1.0
    a.remove_orchestrator("s1")
    assert "s1" not in a._session_last_activity
