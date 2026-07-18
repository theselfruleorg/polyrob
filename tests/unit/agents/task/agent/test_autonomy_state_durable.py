"""Restart-durable autonomy state: the two volatile registries
(AsyncDelegationRegistry, ReentryBudget) persist to autonomy_state.db.

Recovery semantics: a delegation still 'running' at process start was
crash-interrupted — it is marked 'interrupted' and surfaced back to its session
(never silently resumed). ReentryBudget depth carries across restart (a
mid-storm ping-pong loop must not get a free reset by crashing).
"""
import asyncio
import os
import time

import pytest

from agents.task.agent.async_delegation import AsyncDelegationRegistry
from agents.task.agent.autonomy_state import (
    AutonomyStateStore,
    recover_interrupted_delegations,
)
from agents.task.agent.core.self_wake import ReentryBudget


@pytest.fixture()
def store(tmp_path):
    return AutonomyStateStore(str(tmp_path / "autonomy_state.db"))


# --- store primitives ---------------------------------------------------------

def test_delegation_roundtrip(store):
    store.record_dispatched(
        session_id="s1", user_id="u1", delegation_id="deleg_0001",
        goal="do a thing", profile="executor", parent_agent_id="a1",
        dispatched_at=100.0,
    )
    rows = store.list_running()
    assert len(rows) == 1 and rows[0]["session_id"] == "s1"
    store.record_terminal("s1", "deleg_0001", status="completed",
                          completed_at=101.0, result_text="done")
    assert store.list_running() == []


def test_max_counter_seed(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0003",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)
    assert store.max_counter("s1") == 3
    assert store.max_counter("other") == 0


# --- registry persistence -------------------------------------------------------

class _Result:
    success = True
    output_text = "child output"


class _Manager:
    async def run_subtask(self, **kw):
        return _Result()


def test_registry_persists_dispatch_and_terminal(store):
    async def deliver(rec, block):
        pass

    reg = AsyncDelegationRegistry(
        _Manager(), deliver=deliver, store=store, session_id="s1", user_id="u1",
    )

    async def run():
        handle = await reg.dispatch(goal="g", parent_agent_id="a1")
        assert handle["status"] == "dispatched"
        # let the detached task finish
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert store.list_running() == []
    row = store.get("s1", "deleg_0001")
    assert row["status"] == "completed"
    assert "child output" in (row["result_text"] or "")


def test_registry_counter_seeded_from_store(store):
    # Seeding is LAZY (first dispatch, under the lock) so orchestrator
    # construction costs zero sqlite I/O — the restarted session still never
    # reissues a persisted id.
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0007",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)

    async def deliver(rec, block):
        pass

    reg = AsyncDelegationRegistry(
        _Manager(), deliver=deliver, store=store, session_id="s1", user_id="u1",
    )

    async def run():
        handle = await reg.dispatch(goal="g2", parent_agent_id="a1")
        await asyncio.sleep(0.05)
        return handle

    handle = asyncio.run(run())
    assert handle["delegation_id"] == "deleg_0008"


def test_registry_works_without_store():
    async def deliver(rec, block):
        pass

    reg = AsyncDelegationRegistry(_Manager(), deliver=deliver)
    assert reg._next_id() == "deleg_0001"


# --- restart recovery -----------------------------------------------------------

def test_recovery_marks_interrupted_and_surfaces(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="long job", profile="executor",
                            parent_agent_id="a1", dispatched_at=1.0)
    wakes = []

    class _Agent:
        async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
            wakes.append((session_id, user_id, text, metadata))
            return True

    n = asyncio.run(recover_interrupted_delegations(_Agent(), store.db_path))
    assert n == 1
    assert store.list_running() == []
    assert store.get("s1", "deleg_0001")["status"] == "interrupted"
    assert len(wakes) == 1
    sid, uid, text, meta = wakes[0]
    assert sid == "s1" and uid == "u1"
    assert "interrupted" in text.lower()


def test_recovery_fail_open_when_wake_fails(store):
    store.record_dispatched(session_id="s1", user_id="u1", delegation_id="deleg_0001",
                            goal="g", profile="p", parent_agent_id=None, dispatched_at=1.0)

    class _Agent:
        async def deliver_self_wake(self, *a, **kw):
            raise RuntimeError("boom")

    n = asyncio.run(recover_interrupted_delegations(_Agent(), store.db_path))
    # row is still honestly interrupted even though the surface failed
    assert n == 1
    assert store.get("s1", "deleg_0001")["status"] == "interrupted"


def test_recovery_noop_on_missing_db(tmp_path):
    class _Agent:
        async def deliver_self_wake(self, *a, **kw):
            raise AssertionError("must not be called")

    n = asyncio.run(recover_interrupted_delegations(
        _Agent(), str(tmp_path / "nope.db")))
    assert n == 0


# --- ReentryBudget durability ----------------------------------------------------

def test_budget_persists_and_hydrates_across_instances(store):
    b1 = ReentryBudget(3, 0.0, clock=time.time, store=store)
    assert b1.try_consume("s1") is True
    assert b1.try_consume("s1") is True
    # "restart": fresh in-memory state, same store
    b2 = ReentryBudget(3, 0.0, clock=time.time, store=store)
    assert b2.remaining("s1") == 1
    assert b2.try_consume("s1") is True
    assert b2.try_consume("s1") is False  # depth cap carried over


def test_budget_reset_deletes_row(store):
    b1 = ReentryBudget(3, 0.0, clock=time.time, store=store)
    b1.try_consume("s1")
    b1.reset("s1")
    b2 = ReentryBudget(3, 0.0, clock=time.time, store=store)
    assert b2.remaining("s1") == 3


def test_budget_stale_rows_ignored(store):
    old = time.time() - 100 * 86400
    store.put_budget("s1", "", count=3, last_wake_at=old)
    b = ReentryBudget(3, 0.0, clock=time.time, store=store)
    assert b.remaining("s1") == 3  # stale row dropped on hydrate


def test_budget_without_store_unchanged():
    b = ReentryBudget(2, 0.0)
    assert b.try_consume("s1") and b.try_consume("s1")
    assert b.try_consume("s1") is False


def test_record_terminal_cas_does_not_clobber_completed(store):
    """P1 (finalization): the cold-start sweep uses only_if_running=True so it can't
    overwrite a delegation that a concurrent completion already moved to terminal."""
    store.record_dispatched(
        session_id="s1", user_id="u1", delegation_id="deleg_0001",
        goal="g", profile="executor", parent_agent_id=None, dispatched_at=100.0,
    )
    # Genuine completion (authoritative, unconditional).
    n1 = store.record_terminal("s1", "deleg_0001", status="completed",
                               completed_at=101.0, result_text="the real result")
    assert n1 == 1

    # Sweep tries to mark interrupted with the CAS guard — must change 0 rows.
    n2 = store.record_terminal("s1", "deleg_0001", status="interrupted",
                               completed_at=102.0, result_text="restarted",
                               only_if_running=True)
    assert n2 == 0
    row = store.get("s1", "deleg_0001")
    assert row["status"] == "completed"
    assert row["result_text"] == "the real result"


def test_record_terminal_cas_marks_a_still_running_row(store):
    store.record_dispatched(
        session_id="s2", user_id="u1", delegation_id="deleg_0002",
        goal="g", profile="executor", parent_agent_id=None, dispatched_at=100.0,
    )
    n = store.record_terminal("s2", "deleg_0002", status="interrupted",
                              completed_at=102.0, only_if_running=True)
    assert n == 1
    assert store.get("s2", "deleg_0002")["status"] == "interrupted"
