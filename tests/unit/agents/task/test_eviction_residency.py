"""098 AG-08/AG-15: eviction holds the session's locks and leaves the registry
first; the HITL snapshot is merged on restore and survives until the first drain."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.task import session_residency as R
from agents.task.agent.hitl_manager import HITLManager


class _Registry:
    def __init__(self, orch):
        self.d = {"s1": orch}

    def get(self, sid):
        return self.d.get(sid)

    def remove(self, sid):
        self.d.pop(sid, None)

    def count(self):
        return len(self.d)


def _agent(orch):
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)
    a._session_execution_locks = {}
    a._recreate_locks = {}
    a._session_last_activity = {}
    a.telemetry = None
    a.max_sessions_in_memory = 100
    a._registry = _Registry(orch)
    return a


class _SlowOrch:
    def __init__(self):
        self.agents, self.user_id = {}, "u1"
        self.gate = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleaned = False

    async def cleanup(self, **kw):
        self.cleanup_started.set()
        await self.gate.wait()
        self.cleaned = True


@pytest.mark.asyncio
async def test_run_during_eviction_waits_and_recreates(monkeypatch):
    orch = _SlowOrch()
    a = _agent(orch)
    order = []

    async def _recreate(sid, info):
        order.append(("recreate", orch.cleaned))
        new = SimpleNamespace(agents={})
        a._registry.d[sid] = new
        return new
    a._recreate_orchestrator = _recreate
    a._heal_narrowed_request = lambda sid: None

    evict = asyncio.create_task(a._evict_session("s1", reason="ttl"))
    await orch.cleanup_started.wait()
    assert a._registry.get("s1") is None                  # left the registry first
    assert R.residency(a, "s1") == "evicting"
    # a run takes the execution lock the way run_session does: it must wait
    run_lock = a._session_execution_locks.setdefault("s1", asyncio.Lock())
    runner = asyncio.create_task(run_lock.acquire())
    deliver = asyncio.create_task(a._resolve_or_recreate("s1", {}))
    await asyncio.sleep(0.01)
    assert not runner.done() and not deliver.done()
    orch.gate.set()
    await evict
    new = await deliver
    await runner
    run_lock.release()
    assert order == [("recreate", True)]                  # rebuilt only after cleanup ended
    assert a._registry.get("s1") is new


@pytest.mark.asyncio
async def test_no_two_runs_overlap_after_eviction():
    orch = _SlowOrch()
    orch.gate.set()
    a = _agent(orch)
    held = asyncio.Lock()
    a._session_execution_locks["s1"] = held
    waiter = asyncio.create_task(held.acquire())          # a run queued on the lock
    await asyncio.sleep(0)
    await a._evict_session("s1", reason="ttl")
    await waiter
    # the queued run still holds THE lock a new caller gets — no second lock object
    assert a._session_execution_locks.get("s1") is held
    held.release()


@pytest.fixture
def memdir(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_memory_dir", lambda sid, uid: tmp_path)
    return tmp_path


def _hitl():
    return HITLManager(session_id="s1", agent_id="executor_s1")


def _msg(text):
    return {"text": text, "kind": "comment", "metadata": {}, "timestamp": "2026-11-02T10:00:00"}


def test_restore_state_merges_existing_queue():
    h = _hitl()
    h._user_messages.append(_msg("new"))
    h.restore_state({"queued_messages": [_msg("a"), _msg("b")]})
    assert [m["text"] for m in h._user_messages] == ["a", "b", "new"]


@pytest.mark.asyncio
async def test_message_submitted_during_eviction_is_not_lost(memdir):
    orch = SimpleNamespace(user_id="u1", agents={"executor_s1": SimpleNamespace(hitl_manager=_hitl())})
    orch.agents["executor_s1"].hitl_manager._user_messages.append(_msg("queued before"))
    assert R.save_hitl_snapshot(orch, "s1") == 1
    recreated = _hitl()
    recreated._user_messages.append(_msg("arrived during eviction"))   # waited, then delivered
    assert R.restore_hitl_snapshot(recreated, "s1", "u1") == 1
    drained = await recreated.drain_user_messages()
    assert [m["text"] for m in drained] == ["queued before", "arrived during eviction"]


@pytest.mark.asyncio
async def test_restored_snapshot_survives_crash_before_drain(memdir):
    (memdir / R.SNAPSHOT).write_text(json.dumps({"queued_messages": [_msg("keep me")]}))
    first = _hitl()
    R.restore_hitl_snapshot(first, "s1", "u1")
    assert (memdir / R.RESTORED).exists() and not (memdir / R.SNAPSHOT).exists()
    # crash: `first` is gone before any drain; the next recreate restores again
    second = _hitl()
    assert R.restore_hitl_snapshot(second, "s1", "u1") == 1
    drained = await second.drain_user_messages()
    assert [m["text"] for m in drained] == ["keep me"]
    assert not (memdir / R.RESTORED).exists()                         # dropped at the drain


def test_residency_machine_well_formed():
    assert R.RESIDENCY_MACHINE.validate() == []
