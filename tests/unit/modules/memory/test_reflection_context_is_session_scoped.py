"""Reflection's aux model and billing identity must be per-session, not per-process.

`TaskContextManager` is registered as a container SINGLETON
(core/initialization.py, ServiceScope.SINGLETON) and every SessionOrchestrator is
handed the same container, so ONE instance serves every session in the process.
Agent construction used to assign `reflection_llm` / `reflection_meter_ctx` straight
onto that instance, making them last-writer-wins: the second tenant to construct an
Agent silently took over the first tenant's reflection model AND billing identity, so
tenant A's phase reflection ran on tenant B's aux model and was billed to B.

Every other piece of per-session state on this class is already keyed by session_id
(`_sessions`, `_memories_since_reflection`); these two were the exception.
"""
import pytest

from modules.memory.task.task_context_manager import TaskContextManager


@pytest.fixture
def tcm(monkeypatch):
    from core.config import BotConfig

    monkeypatch.setenv("AUTONOMY_STATE_DURABLE", "off")
    return TaskContextManager(name="test-tcm", config=BotConfig())


def _ctx(user):
    return {"usage_tracker": object(), "user_id": user,
            "session_id": f"s_{user}", "agent_id": f"a_{user}", "loop": None}


def test_two_tenants_do_not_clobber_each_other(tcm):
    llm_a, llm_b = object(), object()
    tcm.set_reflection_context("s_alice", llm_a, _ctx("alice"))
    tcm.set_reflection_context("s_bob", llm_b, _ctx("bob"))

    # Alice constructed FIRST and Bob second — the shape that used to lose Alice.
    a = tcm._reflection_by_session["s_alice"]
    b = tcm._reflection_by_session["s_bob"]
    assert a["llm"] is llm_a and b["llm"] is llm_b
    assert a["meter_ctx"]["user_id"] == "alice"
    assert b["meter_ctx"]["user_id"] == "bob"


def test_consolidate_bills_the_session_that_reflected(tcm, monkeypatch):
    """The billing identity handed to ReflectionService must be the reflecting
    session's, not whichever session constructed most recently."""
    seen = {}

    class _FakeService:
        def __init__(self, enabled, llm, meter_ctx):
            seen["llm"] = llm
            seen["meter_ctx"] = meter_ctx

        def consolidate(self, findings):
            return "summary"

    import modules.memory.task.reflection_service as rs
    monkeypatch.setattr(rs, "ReflectionService", _FakeService)

    llm_a, llm_b = object(), object()
    tcm.set_reflection_context("s_alice", llm_a, _ctx("alice"))
    tcm.set_reflection_context("s_bob", llm_b, _ctx("bob"))

    tcm._llm_consolidate(["finding"], session_id="s_alice")
    assert seen["llm"] is llm_a, "Alice's reflection ran on another tenant's model"
    assert seen["meter_ctx"]["user_id"] == "alice", "billed to the wrong tenant"

    tcm._llm_consolidate(["finding"], session_id="s_bob")
    assert seen["llm"] is llm_b
    assert seen["meter_ctx"]["user_id"] == "bob"


def test_unregistered_session_falls_back_to_instance_attributes(tcm, monkeypatch):
    """A dedicated (non-shared) manager, or a caller that never registered, must
    behave exactly as before — the singleton hazard doesn't exist there."""
    seen = {}

    class _FakeService:
        def __init__(self, enabled, llm, meter_ctx):
            seen["llm"] = llm

        def consolidate(self, findings):
            return None

    import modules.memory.task.reflection_service as rs
    monkeypatch.setattr(rs, "ReflectionService", _FakeService)

    legacy = object()
    tcm.reflection_llm = legacy
    tcm._llm_consolidate(["f"], session_id="never_registered")
    assert seen["llm"] is legacy


def test_context_is_dropped_with_the_session(tcm):
    """A long-lived singleton must not accumulate one LLM client per session ever run."""
    tcm.set_reflection_context("s1", object(), _ctx("u"))
    assert "s1" in tcm._reflection_by_session
    tcm.clear_reflection_context("s1")
    assert "s1" not in tcm._reflection_by_session
    tcm.clear_reflection_context("s1")  # idempotent


def test_empty_session_id_is_not_registered(tcm):
    tcm.set_reflection_context("", object(), _ctx("u"))
    assert "" not in tcm._reflection_by_session
