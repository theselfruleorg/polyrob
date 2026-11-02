"""F7 (prod review 2026-10-29): wrong facts entered memory through the phase
summaries of runs that looped or ran out of steps ("six winners paid", a v1
tweet id for a v2 round). Reflection summaries now reach cross-session memory
once, at run end, and only from a run whose stop reason is memory-worthy."""
import logging
from types import SimpleNamespace

import pytest

from agents.task.agent.core.memory_writer import MemoryWriterMixin
from agents.task.runtime.stop_reason import NOT_MEMORY_WORTHY, StopReason, memory_worthy


def test_the_unworthy_stops_are_exactly_the_degenerate_ones():
    assert NOT_MEMORY_WORTHY == {StopReason.MAX_STEPS.value, StopReason.FAILURES.value,
                                 StopReason.LOOP.value}
    assert memory_worthy(StopReason.DONE) and memory_worthy(None)
    assert memory_worthy("awaiting_reply") and not memory_worthy("loop")


def _host(monkeypatch, stop_reason):
    synced = []

    async def fake_sync(task, content, **kw):
        synced.append(content)
        return "inserted"

    monkeypatch.setattr("modules.memory.registry.memory_sync_turn", fake_sync)
    queue = ["[unverified] [phase-summary:execution] Six winners were paid."]

    def drain(sid):
        out, queue[:] = list(queue), []
        return out

    h = MemoryWriterMixin()
    h.orchestrator = None
    h.task_context_manager = SimpleNamespace(
        add_step_memory=lambda **kw: True, get_session=lambda sid: None,
        drain_promoted_findings=drain, release_unsent_findings=lambda *a, **k: True)
    h.state = SimpleNamespace(track_finding=lambda: None, stop_reason=stop_reason)
    h.session_id, h.user_id, h.task = "bad-stop", "owner", "task"
    h.logger = logging.getLogger("bad-stop")
    return h, synced, queue


@pytest.mark.asyncio
async def test_a_step_no_longer_writes_summaries_mid_run(monkeypatch):
    h, synced, queue = _host(monkeypatch, None)
    await h._save_step_to_memory(step_number=1, brain_state={"memory": "x"},
                                 actions=[], results=[])
    assert synced == [] and queue, "a summary was written before the run's end was known"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", sorted(NOT_MEMORY_WORTHY))
async def test_a_degenerate_run_drops_its_summaries(monkeypatch, reason):
    h, synced, queue = _host(monkeypatch, reason)
    await h._sync_reflection_summaries(150)
    assert synced == [] and queue == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["done", "awaiting_reply", "conversational_exit"])
async def test_a_finished_run_writes_its_summaries(monkeypatch, reason):
    h, synced, _ = _host(monkeypatch, reason)
    await h._sync_reflection_summaries(4)
    assert len(synced) == 1
