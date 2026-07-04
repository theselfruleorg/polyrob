"""Task 15 (CO-F7): session-bootstrap injections fire once per SESSION, not per turn.

`run()` resets `self.state.n_steps = 0` on EVERY call, including
`_continue_session=True` conversational turns. The episodic-digest and
continuity-bridge injectors key on `n_steps == 1` — so on a continuing chat
session (same Agent instance reused across turns via `Conversation`, see
`agents/task/agent/conversation.py`) they re-build and re-inject on every
turn, not just the first one of the session.

This test drives a fake Agent (composing the real `RunLoopMixin` +
`MemoryPrefetchMixin`) through two `run()` calls — the second with
`_continue_session=True`, mirroring `Conversation.respond` — and asserts the
digest/bridge builders fire exactly once across both turns, while the
per-turn memory prefetch still fires on both.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.task.agent.core.run_loop import RunLoopMixin
from agents.task.agent.core.memory_prefetch import MemoryPrefetchMixin
from agents.task.agent.views import ActionResult


class _FakeAgent(RunLoopMixin, MemoryPrefetchMixin):
    """Minimal stand-in exercising the real run()/injector interaction."""

    def __init__(self):
        self.task = "chat with the owner"
        self.session_id = "sess-bootstrap-once"
        self.user_id = "u1"
        self.model_name = "test-model"
        self.use_vision = False
        self.logger = logging.getLogger("test-bootstrap-once")
        self.initial_actions = None
        self.generate_gif = False
        self.stall_timeout_seconds = None
        self._cancelled = False
        self._last_result = []
        self._stall_check_task = None
        self.orchestrator = None
        self.register_done_callback = None
        self.validate_output = False
        self._is_sub_agent = False
        self.usage_tracker = None
        self.container = MagicMock()

        self.state = MagicMock()
        self.state.n_steps = 0
        self.state.consecutive_failures = 0

        self.telemetry_manager = MagicMock()

        self._created_task_context_manager = False
        self.task_context_manager = MagicMock()
        self.task_context_manager.load_session.return_value = {"loaded": True}

        self.message_manager = MagicMock()
        self.message_manager.get_token_count.return_value = 0

        self._drain_user_messages = AsyncMock(return_value=[])
        self._handle_control_flags = AsyncMock(return_value=False)
        self._check_context_overflow = MagicMock(return_value=False)
        self._too_many_failures = MagicMock(return_value=False)

        self.history = MagicMock()
        self.history.history = []
        self.history.is_done.return_value = True
        self.history.errors.return_value = []
        self.agent_id = "agent-1"

    async def step(self, step_info=None):
        """Stand-in for the real step.py flow: bump n_steps, run the three
        bootstrap/prefetch injectors exactly as step.py does, then finish.

        Mirrors the CO-F7 fix: the bootstrap-done flag is set here (right
        after the injectors run on n_steps == 1), NOT unconditionally at the
        end of run() — so a run() that breaks before step() ever executes
        never sets it.
        """
        self.state.n_steps = step_info.step_number + 1
        await self._maybe_prefetch_memory()
        await self._maybe_inject_episodic_digest()
        await self._maybe_inject_continuity_bridge()
        if self.state.n_steps == 1:
            self._session_bootstrap_done = True
        self._last_result = [
            ActionResult(is_done=True, extracted_content="ok", success=True)
        ]


@pytest.mark.asyncio
async def test_digest_and_bridge_fire_once_per_session_not_per_turn():
    agent = _FakeAgent()

    registry = MagicMock()
    registry.resolve_by_session_id.return_value = {"session_key": "thread-1"}
    agent.container.get_service.return_value = registry

    with patch(
        "agents.task.agent.core.episodic_digest.build_activity_digest",
        new=AsyncMock(return_value=None),
    ) as digest_mock, patch(
        "core.surfaces.continuity.build_bridge_message",
        new=AsyncMock(return_value=None),
    ) as bridge_mock, patch(
        "agents.task.agent.core.memory_prefetch.build_prefetch_message",
        new=AsyncMock(return_value=None),
    ) as prefetch_mock, patch(
        "agents.task.constants.AutonomyConfig.continuity_bridge_enabled",
        return_value=True,
    ):
        # Turn 1 — fresh session.
        await agent.run(max_steps=5, _continue_session=False)
        # Turn 2 — same agent instance, continuing session (mirrors
        # Conversation.respond passing _continue_session=bool(self.turns)).
        await agent.run(max_steps=5, _continue_session=True)

    assert digest_mock.call_count == 1, (
        "episodic digest builder must fire once per SESSION, not per turn"
    )
    assert bridge_mock.call_count == 1, (
        "continuity bridge builder must fire once per SESSION, not per turn"
    )
    # Per-turn memory prefetch is intentionally unchanged: fires on n_steps==1
    # of EVERY turn.
    assert prefetch_mock.call_count == 2, (
        "memory prefetch must still fire on the first step of every turn"
    )


@pytest.mark.asyncio
async def test_early_exit_before_step_leaves_bootstrap_undone_then_next_turn_injects():
    """Turn 1's run() breaks BEFORE step() ever executes (e.g. cancellation).

    n_steps never reaches 1, so the injectors never get their chance. The
    bootstrap flag must stay False (not be set unconditionally at the end of
    run()) so a later real turn still gets to inject once — the bug this test
    guards against would permanently suppress the digest + continuity bridge
    for the rest of the session.
    """
    agent = _FakeAgent()
    # Force the run loop to break on its very first iteration, before step()
    # is ever called (mirrors cancellation / resumed-from-done-or-stopped /
    # too-many-failures early-exit paths in run_loop.py).
    agent._cancelled = True

    registry = MagicMock()
    registry.resolve_by_session_id.return_value = {"session_key": "thread-1"}
    agent.container.get_service.return_value = registry

    with patch(
        "agents.task.agent.core.episodic_digest.build_activity_digest",
        new=AsyncMock(return_value=None),
    ) as digest_mock, patch(
        "core.surfaces.continuity.build_bridge_message",
        new=AsyncMock(return_value=None),
    ) as bridge_mock, patch(
        "agents.task.agent.core.memory_prefetch.build_prefetch_message",
        new=AsyncMock(return_value=None),
    ) as prefetch_mock, patch(
        "agents.task.constants.AutonomyConfig.continuity_bridge_enabled",
        return_value=True,
    ):
        # Turn 1 — cancelled before step() ever runs.
        await agent.run(max_steps=5, _continue_session=False)

        assert digest_mock.call_count == 0, (
            "digest builder must not fire when step() never executed"
        )
        assert bridge_mock.call_count == 0, (
            "bridge builder must not fire when step() never executed"
        )
        assert prefetch_mock.call_count == 0
        assert getattr(agent, "_session_bootstrap_done", False) is False, (
            "bootstrap flag must stay False when turn 1 broke before step 1 ran"
        )

        # Turn 2 — a real, uncancelled continuation of the same session must
        # still get its one shot at the digest + continuity bridge.
        agent._cancelled = False
        await agent.run(max_steps=5, _continue_session=True)

    assert digest_mock.call_count == 1, (
        "the next real turn must still inject the digest exactly once"
    )
    assert bridge_mock.call_count == 1, (
        "the next real turn must still inject the continuity bridge exactly once"
    )
    assert prefetch_mock.call_count == 1
    assert agent._session_bootstrap_done is True
