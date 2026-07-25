"""T1.4 (Task 2): single-flight dead-session correspondent resume.

Two concurrent ``_try_conversation_resume`` calls for the SAME dead
``orig_session_id`` must not double-create sessions / double-rebind the
correspondent registry. Without the ``_resume_locks`` serialization, both
in-flight callers see the correspondent registry still pointing at the dead
session, both call ``create_session``, and one of the two rebinds gets
clobbered/orphaned (the reply that landed in the losing session is never
seen again).

Reproduces the race with an ``asyncio.Event``-gated ``create_session`` (both
callers provably in-flight together, mirroring
tests/unit/agents/task/agent/test_async_delegation.py:124-142's gate idiom)
and asserts the fix collapses it to exactly one create + one rebind, with the
loser delivered into the winner's new session.
"""
import asyncio
import types

import pytest

from agents.task_agent_lite import TaskAgent


class _Orch:
    """Fake orchestrator: records every injected correspondent message."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.injected = []

    def inject_correspondent_message(self, text, source, metadata=None, *,
                                      surface=None, address=None):
        self.injected.append({"text": text, "source": source, "surface": surface,
                               "address": address})
        return True


class _CorrespondentRegistry:
    """Fake correspondent registry: (surface, address) -> current session_id.

    ``resolve`` always reflects the CURRENT binding (mutated in place by
    ``rebind_session``), exactly like the real sqlite-backed registry — this is
    what lets the post-lock re-check in ``_try_conversation_resume`` observe a
    winner's rebind.
    """

    def __init__(self, *, user_id, session_id):
        self._user_id = user_id
        self._session_id = session_id
        self.rebind_calls = []

    def resolve(self, *, surface, address):
        return {"user_id": self._user_id, "session_id": self._session_id}

    def rebind_session(self, *, surface, address, user_id, new_session_id):
        self.rebind_calls.append(new_session_id)
        self._session_id = new_session_id
        return 1


def _build_agent(*, registry, orch_map, gate):
    """Bare TaskAgent (object.__new__, no __init__) pinned to exactly the
    attributes ``_try_conversation_resume`` touches."""
    agent = object.__new__(TaskAgent)
    agent.container = types.SimpleNamespace(
        get_service=lambda name: registry if name == "correspondent_registry" else None
    )
    agent.session_manager = types.SimpleNamespace(
        get_session_info=lambda sid: {}
    )
    agent._registry = types.SimpleNamespace(get=lambda sid: orch_map.get(sid))

    run_calls = []

    async def _run_session(user_id, session_id=None):
        run_calls.append((user_id, session_id))
        return "ok"
    agent.run_session = _run_session
    agent._run_calls = run_calls

    async def _resolve_or_recreate(session_id, session_info):
        return orch_map.get(session_id)
    agent._resolve_or_recreate = _resolve_or_recreate

    counter = {"n": 0}

    async def _create_session(user_id, request, *a, **k):
        await gate.wait()  # both callers must be in-flight together
        counter["n"] += 1
        new_sid = f"new-sess-{counter['n']}"
        orch_map[new_sid] = _Orch(new_sid)
        return {"session_id": new_sid}

    from unittest.mock import AsyncMock
    agent.create_session = AsyncMock(side_effect=_create_session)
    return agent


@pytest.mark.asyncio
async def test_concurrent_resume_is_single_flight(monkeypatch):
    monkeypatch.delenv("CONVERSATION_RESUME_ENABLED", raising=False)
    gate = asyncio.Event()
    registry = _CorrespondentRegistry(user_id="t1", session_id="dead-sess")
    orch_map = {}
    agent = _build_agent(registry=registry, orch_map=orch_map, gate=gate)

    task_a = asyncio.ensure_future(agent._try_conversation_resume(
        "dead-sess", "john@acme.com", "message A", surface="email"))
    task_b = asyncio.ensure_future(agent._try_conversation_resume(
        "dead-sess", "john@acme.com", "message B", surface="email"))

    # Let both reach the gated create_session call before releasing it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()

    ok_a, ok_b = await asyncio.gather(task_a, task_b)

    assert ok_a is True and ok_b is True
    # Exactly one replacement session was minted...
    agent.create_session.assert_awaited_once()
    # ...and exactly one rebind of the correspondent registry happened.
    assert len(registry.rebind_calls) == 1
    winner_sid = registry.rebind_calls[0]
    assert registry.resolve(surface="email", address="john@acme.com")["session_id"] == winner_sid

    # Both messages landed — the loser was delivered into the WINNER's session,
    # never into a second, orphaned session.
    winner_orch = orch_map[winner_sid]
    injected_texts = " ".join(i["text"] for i in winner_orch.injected)
    assert "message A" in injected_texts
    assert "message B" in injected_texts
    for sid, orch in orch_map.items():
        if sid != winner_sid:
            assert not orch.injected, f"a second session {sid} was minted and used"

    await asyncio.sleep(0)  # let the detached run_session calls start
    assert all(sid == winner_sid for _, sid in agent._run_calls)


@pytest.mark.asyncio
async def test_single_caller_unchanged(monkeypatch):
    """Regression guard: a lone resume (no race) behaves exactly as before —
    one create_session, one rebind, delivered into the new session."""
    monkeypatch.delenv("CONVERSATION_RESUME_ENABLED", raising=False)
    gate = asyncio.Event()
    gate.set()  # no gating needed for the single-caller case
    registry = _CorrespondentRegistry(user_id="t1", session_id="dead-sess")
    orch_map = {}
    agent = _build_agent(registry=registry, orch_map=orch_map, gate=gate)

    ok = await agent._try_conversation_resume(
        "dead-sess", "john@acme.com", "hello again", surface="email")

    assert ok is True
    agent.create_session.assert_awaited_once()
    assert len(registry.rebind_calls) == 1
    new_sid = registry.rebind_calls[0]
    assert orch_map[new_sid].injected[0]["text"] == "hello again"
    await asyncio.sleep(0)
    assert agent._run_calls == [("t1", new_sid)]
