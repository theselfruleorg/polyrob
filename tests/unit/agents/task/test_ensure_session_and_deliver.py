"""TaskAgent.ensure_session_and_deliver — the resident-or-recreate delivery rail
that the Telegram STEER path uses to resume the BOUND session (restoring history)
instead of minting a new amnesiac session (P0.2 Fix A)."""
from types import SimpleNamespace

import pytest


class _Orch:
    def __init__(self):
        self.submitted = []
    async def submit_user_message(self, agent_id, text, kind="comment", metadata=None):
        self.submitted.append((agent_id, text, kind, metadata))


def _agent(*, session_info, resident=None, recreated=None):
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)  # bypass __init__
    a.session_manager = SimpleNamespace(get_session_info=lambda sid: session_info)
    a._registry = SimpleNamespace(get=lambda sid: resident)
    async def _recreate(sid, info):
        return recreated
    a._recreate_orchestrator = _recreate
    a._recreate_locks = {}
    return a


@pytest.mark.asyncio
async def test_delivers_to_resident_session():
    orch = _Orch()
    a = _agent(session_info={"user_id": "u1"}, resident=orch)
    status = await a.ensure_session_and_deliver("u1", "s1", "hi")
    assert status == "delivered"
    assert orch.submitted and orch.submitted[0][1] == "hi"


@pytest.mark.asyncio
async def test_recreates_evicted_session_and_delivers():
    """The core fix: an evicted session is recreated-from-disk and the message delivered."""
    orch = _Orch()
    a = _agent(session_info={"user_id": "u1"}, resident=None, recreated=orch)
    status = await a.ensure_session_and_deliver("u1", "s1", "where were we?")
    assert status == "delivered"
    assert orch.submitted[0][1] == "where were we?"


@pytest.mark.asyncio
async def test_truly_gone_returns_gone():
    a = _agent(session_info=None)  # no on-disk metadata
    status = await a.ensure_session_and_deliver("u1", "s1", "hi")
    assert status == "gone"


@pytest.mark.asyncio
async def test_not_recreatable_returns_gone():
    a = _agent(session_info={"user_id": "u1"}, resident=None, recreated=None)
    status = await a.ensure_session_and_deliver("u1", "s1", "hi")
    assert status == "gone"


@pytest.mark.asyncio
async def test_tenant_mismatch_refused():
    orch = _Orch()
    a = _agent(session_info={"user_id": "other"}, resident=orch)
    status = await a.ensure_session_and_deliver("u1", "s1", "hi")
    assert status == "gone"
    assert orch.submitted == []  # never delivered cross-tenant


@pytest.mark.asyncio
async def test_failed_recreate_does_not_leak_recreate_lock():
    """Fusion M3/L1: a session that never becomes resident (recreate -> None) must not
    leave a _recreate_locks entry behind (eviction's pop is gated on a live orchestrator)."""
    a = _agent(session_info={"user_id": "u1"}, resident=None, recreated=None)
    status = await a.ensure_session_and_deliver("u1", "s1", "hi")
    assert status == "gone"
    assert "s1" not in a._recreate_locks  # lock dropped on failed recreate


@pytest.mark.asyncio
async def test_queue_full_returns_busy_not_gone():
    """a-MED2: a full message queue means the session is ALIVE and processing — return
    'busy' (the surface should say 'still working', NOT mint a fresh amnesiac session)."""
    from core.exceptions import MessageQueueFullError

    class _FullOrch:
        async def submit_user_message(self, *a, **k):
            raise MessageQueueFullError("full", queue_size=10, max_size=10)

    a = _agent(session_info={"user_id": "u1"}, resident=_FullOrch())
    status = await a.ensure_session_and_deliver("u1", "s1", "hi")
    assert status == "busy"


@pytest.mark.asyncio
async def test_concurrent_deliver_recreates_once():
    """HIGH bug fix: two messages racing on an evicted session must recreate the
    orchestrator exactly ONCE (a per-session lock + re-check), and BOTH messages must
    land in the same orchestrator — not lost into an orphaned double-build."""
    import asyncio
    from agents.task_agent_lite import TaskAgent
    a = object.__new__(TaskAgent)
    a.session_manager = SimpleNamespace(get_session_info=lambda sid: {"user_id": "u1"})
    state = {"orch": None}
    a._registry = SimpleNamespace(get=lambda sid: state["orch"])
    calls = {"n": 0}
    orch = _Orch()
    async def _recreate(sid, info):
        calls["n"] += 1
        await asyncio.sleep(0.01)   # widen the race window
        state["orch"] = orch        # "register" the recreated orchestrator
        return orch
    a._recreate_orchestrator = _recreate
    a._recreate_locks = {}
    await asyncio.gather(
        a.ensure_session_and_deliver("u1", "s1", "m1"),
        a.ensure_session_and_deliver("u1", "s1", "m2"),
    )
    assert calls["n"] == 1                 # recreated exactly once
    assert len(orch.submitted) == 2        # both messages delivered to the SAME orch
