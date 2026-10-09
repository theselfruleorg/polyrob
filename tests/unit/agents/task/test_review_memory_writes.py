import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.task.agent.core.memory_writer import MemoryWriterMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["_public_session", "_correspondent_session", "_correspondent_tainted", "owner"])
async def test_third_party_findings_never_promote_to_owner_memory(monkeypatch, marker):
    sync = AsyncMock()
    monkeypatch.setattr("modules.memory.registry.memory_sync_turn", sync)
    host = MemoryWriterMixin()
    host.orchestrator = SimpleNamespace(**{marker: True})
    host.task_context_manager = SimpleNamespace(
        add_step_memory=lambda **kw: True,
        drain_promoted_findings=lambda sid: ["third-party instruction"],
    )
    host.session_id, host.user_id, host.task = "review-memory", "owner", "task"
    host.logger = logging.getLogger("review-memory")
    await host._save_step_to_memory(
        step_number=1, brain_state={"memory": "finding", "phase": "discovery"},
        actions=[], results=[])
    assert sync.await_count == (1 if marker == "owner" else 0)
