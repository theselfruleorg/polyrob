"""098 AG-10: the delegation boot sweep only takes rows whose owner is provably gone."""
import asyncio
import os
import subprocess
import sys
import time

import pytest

from agents.task.agent.autonomy_state import (
    DELEGATION_MACHINE, AutonomyStateStore, recover_interrupted_delegations)
from core.state.proc import ProcIdentity, boot_id, process_identity, start_time


@pytest.fixture
def store(tmp_path):
    return AutonomyStateStore(str(tmp_path / "autonomy_state.db"))


class _Agent:
    def __init__(self):
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append(session_id)
        return True


def _dispatch(store, did, **kw):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id=did, goal="g",
                            profile="p", parent_agent_id=None,
                            dispatched_at=kw.pop("dispatched_at", time.time()), **kw)


def _sweep(store):
    agent = _Agent()
    n = asyncio.run(recover_interrupted_delegations(agent, store.db_path))
    return n, agent.wakes


def test_table_well_formed():
    assert DELEGATION_MACHINE.validate() == []


def test_sweep_skips_row_of_live_other_process(store):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = None
        for _ in range(50):
            st = start_time(p.pid)
            if st:
                break
            time.sleep(0.05)
        _dispatch(store, "deleg_0001", owner=ProcIdentity(p.pid, st, boot_id()).encode())
        n, wakes = _sweep(store)
        assert n == 0 and wakes == []
        assert store.get("s1", "deleg_0001")["status"] == "running"
    finally:
        p.kill()
        p.wait()


def test_sweep_skips_this_process_live_row(store):
    _dispatch(store, "deleg_0001")                      # owner = this process
    assert _sweep(store)[0] == 0


def test_sweep_marks_row_of_dead_pid(store):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    st = start_time(p.pid) or "gone"
    p.wait()
    _dispatch(store, "deleg_0001", owner=ProcIdentity(p.pid, st, boot_id()).encode())
    n, wakes = _sweep(store)
    assert n == 1 and wakes == ["s1"]
    assert store.get("s1", "deleg_0001")["status"] == "interrupted"


def test_sweep_marks_own_previous_boot_row(store):
    me = process_identity()
    _dispatch(store, "deleg_0001", owner=ProcIdentity(me.pid, me.start, "old-boot").encode())
    assert _sweep(store)[0] == 1


def test_legacy_row_without_owner_marked_only_after_timeout(store):
    _dispatch(store, "deleg_0001", owner="", dispatched_at=time.time())
    _dispatch(store, "deleg_0002", owner="", dispatched_at=time.time() - 7200)
    n, _ = _sweep(store)
    assert n == 1
    assert store.get("s1", "deleg_0001")["status"] == "running"
    assert store.get("s1", "deleg_0002")["status"] == "interrupted"


def test_late_completion_wins_over_interrupted(store):
    _dispatch(store, "deleg_0001", owner="", dispatched_at=1.0)
    assert _sweep(store)[0] == 1
    assert store.record_terminal("s1", "deleg_0001", status="completed",
                                 completed_at=time.time(), result_text="done") == 1
    assert store.get("s1", "deleg_0001")["status"] == "completed"


def test_cancelled_row_is_never_overwritten(store):
    _dispatch(store, "deleg_0001")
    assert store.record_terminal("s1", "deleg_0001", status="cancelled", completed_at=1.0)
    assert store.record_terminal("s1", "deleg_0001", status="completed",
                                 completed_at=2.0) == 0
    assert store.get("s1", "deleg_0001")["status"] == "cancelled"
