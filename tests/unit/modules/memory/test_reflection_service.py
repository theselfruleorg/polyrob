"""TDD tests for ReflectionService (Part A).

Tests are written first (RED), then the implementation is created to make them GREEN.

ReflectionService owns the _llm_consolidate logic extracted from TaskContextManager:
- LLM-synthesized phase consolidation via aux model
- Fallback to None on error (caller falls back to "; ".join concat)
- REFLECTION_LLM_ENABLED gate
- event=reflection_consolidate / event=reflection_fallback breadcrumbs
"""
import asyncio
import logging
import threading
import pytest
from modules.llm.messages import AIMessage


class _StubLLM:
    """Minimal LLM stub that mimics the ainvoke interface."""

    def __init__(self, reply="CONSOLIDATED SUMMARY", fail=False, empty=False):
        self.reply = reply
        self.fail = fail
        self.empty = empty
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("aux model down")
        content = "" if self.empty else self.reply
        return AIMessage(content=content)


# ---------------------------------------------------------------------------
# Test 1: enabled + working aux LLM → returns LLM-consolidated result
# ---------------------------------------------------------------------------

def test_consolidate_returns_llm_result_when_enabled():
    """With REFLECTION_LLM_ENABLED on and a working aux LLM, returns the model's text."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM(reply="Synthesized phase summary from LLM")
    svc = ReflectionService(enabled=True, llm=llm)

    result = svc.consolidate(["finding A", "finding B", "finding C"])

    assert result == "Synthesized phase summary from LLM"
    assert len(llm.calls) == 1, "aux model should have been called exactly once"


# ---------------------------------------------------------------------------
# Test 2: aux LLM raises → falls back to None (caller does "; ".join concat)
# ---------------------------------------------------------------------------

def test_consolidate_falls_back_to_none_on_error():
    """When the aux LLM raises, consolidate() returns None — caller uses concat."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM(fail=True)
    svc = ReflectionService(enabled=True, llm=llm)

    result = svc.consolidate(["finding X"])

    assert result is None, "error in aux model should produce None (caller concat fallback)"


# ---------------------------------------------------------------------------
# Additional edge-case tests (extend coverage without changing the contract)
# ---------------------------------------------------------------------------

def test_consolidate_returns_none_when_disabled():
    """When enabled=False, consolidate() skips the LLM and returns None immediately."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM()
    svc = ReflectionService(enabled=False, llm=llm)

    result = svc.consolidate(["finding A"])

    assert result is None
    assert len(llm.calls) == 0, "LLM must NOT be called when disabled"


def test_consolidate_returns_none_when_llm_is_none():
    """When no llm is provisioned, consolidate() returns None (fail-open)."""
    from modules.memory.task.reflection_service import ReflectionService

    svc = ReflectionService(enabled=True, llm=None)

    result = svc.consolidate(["finding A"])

    assert result is None


def test_consolidate_returns_none_on_empty_findings():
    """When findings list is empty, skip the LLM call and return None."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM()
    svc = ReflectionService(enabled=True, llm=llm)

    result = svc.consolidate([])

    assert result is None
    assert len(llm.calls) == 0


def test_consolidate_returns_none_on_empty_llm_response():
    """When the LLM returns empty content, consolidate() returns None."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM(empty=True)
    svc = ReflectionService(enabled=True, llm=llm)

    result = svc.consolidate(["finding A"])

    assert result is None


def test_consolidate_breadcrumbs_on_success(caplog):
    """Successful consolidation emits event=reflection_consolidate breadcrumb."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM(reply="SYNTH")
    svc = ReflectionService(enabled=True, llm=llm)

    with caplog.at_level(logging.INFO, logger="modules.memory.task.reflection_service"):
        result = svc.consolidate(["f1", "f2"])

    assert result == "SYNTH"
    assert any("reflection_consolidate" in r.message for r in caplog.records), (
        "expected event=reflection_consolidate breadcrumb in logs"
    )


def test_consolidate_breadcrumbs_on_error(caplog):
    """Failed consolidation emits event=reflection_fallback breadcrumb."""
    from modules.memory.task.reflection_service import ReflectionService

    llm = _StubLLM(fail=True)
    svc = ReflectionService(enabled=True, llm=llm)

    with caplog.at_level(logging.WARNING, logger="modules.memory.task.reflection_service"):
        result = svc.consolidate(["f1"])

    assert result is None
    assert any("reflection_fallback" in r.message for r in caplog.records), (
        "expected event=reflection_fallback breadcrumb in logs"
    )


# ---------------------------------------------------------------------------
# Important 5: reflection metering loop-affinity.
#
# Reflection consolidation runs on a WORKER thread (add_step_memory is offloaded
# via asyncio.to_thread), so run_coroutine_sync spins a throwaway loop there. The
# usage tracker's DB is bound to the MAIN loop, so metering on the throwaway loop
# raises "bound to a different event loop" and is silently swallowed → never
# billed. The fix schedules the meter back onto the captured main loop.
# ---------------------------------------------------------------------------

class _LoopBoundTracker:
    """Fake usage tracker that mimics a MAIN-loop-affine DB resource: it refuses
    to run on any loop other than the one it was bound to (like the aiosqlite
    connection's asyncio.Lock)."""

    def __init__(self, expected_loop):
        self.expected_loop = expected_loop
        self.calls = []

    async def record_llm_usage(self, **kwargs):
        running = asyncio.get_running_loop()
        if running is not self.expected_loop:
            raise RuntimeError("record_llm_usage bound to a different event loop")
        self.calls.append(kwargs)


@pytest.fixture()
def _bg_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)
    loop.close()


def test_reflection_meter_scheduled_onto_captured_loop_bills(_bg_loop):
    """With meter_ctx['loop'] set to the main loop, the aux call IS billed even
    though consolidate runs off that loop (the loop-affinity fix)."""
    from modules.memory.task.reflection_service import ReflectionService

    tracker = _LoopBoundTracker(expected_loop=_bg_loop)
    meter_ctx = {
        "usage_tracker": tracker, "user_id": "u1", "session_id": "s1",
        "agent_id": "a1", "loop": _bg_loop,
    }
    llm = _StubLLM(reply="SYNTH")
    svc = ReflectionService(enabled=True, llm=llm, meter_ctx=meter_ctx)

    # Called from a thread with NO running loop (mimics the worker thread).
    result = svc.consolidate(["f1", "f2"])

    assert result == "SYNTH"                # reflection summary preserved
    assert len(tracker.calls) == 1          # and it was actually billed
    assert tracker.calls[0]["user_id"] == "u1"
    assert isinstance(tracker.calls[0]["duration_seconds"], float)


def test_reflection_meter_without_loop_silently_drops_on_affine_tracker(_bg_loop):
    """Legacy path (no captured loop) demonstrates the bug: a main-loop-affine
    tracker raises on the throwaway loop and the fail-open swallow means the call
    is NEVER billed — yet the reflection summary still survives."""
    from modules.memory.task.reflection_service import ReflectionService

    tracker = _LoopBoundTracker(expected_loop=_bg_loop)  # bound elsewhere
    meter_ctx = {
        "usage_tracker": tracker, "user_id": "u1", "session_id": "s1",
        "agent_id": "a1",  # NO "loop"
    }
    llm = _StubLLM(reply="SYNTH")
    svc = ReflectionService(enabled=True, llm=llm, meter_ctx=meter_ctx)

    result = svc.consolidate(["f1"])

    assert result == "SYNTH"        # summary preserved (meter failure isolated)
    assert tracker.calls == []      # silently dropped — this is the bug the fix cures


def test_reflection_meter_failure_does_not_drop_summary(_bg_loop):
    """A metering exception must never discard the already-computed summary."""
    from modules.memory.task.reflection_service import ReflectionService

    class _Boom:
        async def record_llm_usage(self, **kwargs):
            raise RuntimeError("db exploded")

    meter_ctx = {
        "usage_tracker": _Boom(), "user_id": "u1", "session_id": "s1",
        "agent_id": "a1", "loop": _bg_loop,
    }
    svc = ReflectionService(enabled=True, llm=_StubLLM(reply="KEEP ME"), meter_ctx=meter_ctx)
    assert svc.consolidate(["f1"]) == "KEEP ME"
