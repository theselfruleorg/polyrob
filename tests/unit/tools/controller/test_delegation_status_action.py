"""120 W2: `delegation_status` — the agent lists and stops its own background helpers."""
import asyncio
import types

import pytest

from agents.task.agent.async_delegation import AsyncDelegationRegistry, DelegationRecord


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model)
            return fn
        return deco


def _action(orchestrator):
    from tools.controller.delegation_status_action import register_delegation_status_action
    c = types.SimpleNamespace(registry=_Registry(), orchestrator=orchestrator)
    register_delegation_status_action(c)
    fn, model = c.registry.actions["delegation_status"]
    return fn, model


def _turn(leaf=False):
    return types.SimpleNamespace(user_id="alice", role="leaf" if leaf else "orchestrator",
                                 is_sub_agent=leaf, metadata={}, session_id="s1")


async def _deliver(rec, text):
    return None


async def _registry_with_helpers():
    reg = AsyncDelegationRegistry(manager=object(), deliver=_deliver)
    ev = asyncio.Event()

    async def body():
        ev.set()
        await asyncio.sleep(30)

    t = asyncio.create_task(body())
    await ev.wait()
    reg._records["deleg_0001"] = DelegationRecord(
        delegation_id="deleg_0001", goal="research the fee schedule", profile="executor",
        dispatched_at=1.0, task=t)
    reg._records["deleg_0002"] = DelegationRecord(
        delegation_id="deleg_0002", goal="summarise the thread", profile="executor",
        status="completed", dispatched_at=2.0, completed_at=5.0)
    return reg, t


@pytest.mark.asyncio
async def test_no_registry_says_not_available():
    fn, model = _action(types.SimpleNamespace())
    res = await fn(model(), execution_context=_turn())
    assert res.error and "not available in this session" in res.error


@pytest.mark.asyncio
async def test_list_is_this_sessions_helpers_and_says_so():
    reg, t = await _registry_with_helpers()
    fn, model = _action(types.SimpleNamespace(async_delegation=reg))
    res = await fn(model(), execution_context=_turn())
    text = res.extracted_content
    assert "THIS session" in text and "other sessions are not listed" in text
    assert "deleg_0001 [running]" in text and "deleg_0002 [completed] 3s" in text
    assert text.index("deleg_0002") < text.index("deleg_0001")  # newest first
    t.cancel()


@pytest.mark.asyncio
async def test_cancel_one_helper():
    reg, t = await _registry_with_helpers()
    fn, model = _action(types.SimpleNamespace(async_delegation=reg))
    res = await fn(model(cancel="deleg_0001", reason="no longer needed"),
                   execution_context=_turn())
    await asyncio.sleep(0)
    assert not res.error and "Cancelled background helper deleg_0001" in res.extracted_content
    assert t.cancelled() and reg._records["deleg_0001"].status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_of_a_finished_helper_says_nothing_to_stop():
    reg, t = await _registry_with_helpers()
    fn, model = _action(types.SimpleNamespace(async_delegation=reg))
    res = await fn(model(cancel="deleg_0002"), execution_context=_turn())
    assert not res.error and "nothing to stop" in res.extracted_content
    t.cancel()


@pytest.mark.asyncio
async def test_an_id_of_another_session_is_not_found_here():
    reg, t = await _registry_with_helpers()
    fn, model = _action(types.SimpleNamespace(async_delegation=reg))
    res = await fn(model(cancel="deleg_0042"), execution_context=_turn())
    assert res.error and "this session's only" in res.error
    t.cancel()


@pytest.mark.asyncio
async def test_a_leaf_cannot_cancel_but_may_list():
    reg, t = await _registry_with_helpers()
    fn, model = _action(types.SimpleNamespace(async_delegation=reg))
    res = await fn(model(cancel="deleg_0001"), execution_context=_turn(leaf=True))
    assert res.error and "leaf" in res.error and not t.cancelled()
    res = await fn(model(), execution_context=_turn(leaf=True))
    assert not res.error
    t.cancel()


def test_the_action_has_its_policy_row():
    from core.verb_policy import policy_for
    row = policy_for("delegation_status")
    assert row is not None and row.room_denied and row.correspondent_blocked
