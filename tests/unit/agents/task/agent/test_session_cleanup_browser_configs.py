"""Regression test for the full_cleanup browser-config-forgetting wiring in
SessionCleanupMixin.cleanup().

The behavior under test (added alongside BrowserManager.forget_session_config,
commit 66f12445): on a FULL session teardown, cleanup() must forget the cached
BrowserManager.session_configs entry for EVERY agent_id the session ever
created (self.agents.keys()) -- not just the smaller set of currently-tracked
browser contexts (self._browser_contexts), which only contains agent_ids whose
context is still allocated. And it must only do this when full_cleanup=True.

Without this test, someone could "simplify" the loop back to iterating
self._browser_contexts (silently under-covering agents whose context was
already released in an earlier turn), or drop the full_cleanup gate, and no
test would catch it.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.session.cleanup import SessionCleanupMixin


class _FakeBrowserManager:
    """Minimal BrowserManager double: records forget_session_config calls."""

    def __init__(self):
        self.release_context = AsyncMock()
        self.forget_session_config = MagicMock()


class _MinimalOrchestrator(SessionCleanupMixin):
    """Smallest object that composes SessionCleanupMixin and satisfies every
    attribute cleanup() touches, given the defaults used in these tests
    (controller=None, session_manager=None skip their respective blocks;
    no telemetry_manager/llm_clients/_execution_tasks attrs -> those optional
    blocks no-op via hasattr checks)."""

    def __init__(self, agents, browser_contexts):
        self.session_id = "sess-1"
        self.user_id = "user-1"
        self.agents = agents
        self._browser_contexts = set(browser_contexts)
        self.browser_manager = _FakeBrowserManager()
        self.controller = None
        self.session_manager = None
        self.logger = MagicMock()


def _make_agents():
    # Three agent_ids the session created; plain objects so every hasattr()
    # probe in cleanup()'s full_cleanup persistence block (message_manager,
    # state, task_context_manager, hitl_manager, cleanup, ...) is False and
    # those blocks safely no-op.
    return {"agent-1": object(), "agent-2": object(), "agent-3": object()}


@pytest.mark.asyncio
async def test_full_cleanup_forgets_browser_config_for_every_agent_not_just_tracked_contexts():
    """Crux regression test: _browser_contexts is a STRICT SUBSET of
    self.agents.keys(). If the wiring iterated self._browser_contexts instead
    of self.agents.keys(), only 'agent-1' would get forget_session_config
    called and 'agent-2'/'agent-3' would be silently missed.
    """
    agents = _make_agents()
    orchestrator = _MinimalOrchestrator(agents, browser_contexts={"agent-1"})

    await orchestrator.cleanup(full_cleanup=True)

    forgotten_ids = {
        call.args[0]
        for call in orchestrator.browser_manager.forget_session_config.call_args_list
    }
    assert forgotten_ids == {"agent-1", "agent-2", "agent-3"}
    assert orchestrator.browser_manager.forget_session_config.call_count == 3


@pytest.mark.asyncio
async def test_partial_cleanup_does_not_forget_browser_configs():
    """The forget_session_config sweep must be gated on full_cleanup=True --
    a partial (continuous-chat) cleanup must not touch cached configs."""
    agents = _make_agents()
    orchestrator = _MinimalOrchestrator(agents, browser_contexts={"agent-1"})

    await orchestrator.cleanup(full_cleanup=False)

    orchestrator.browser_manager.forget_session_config.assert_not_called()
