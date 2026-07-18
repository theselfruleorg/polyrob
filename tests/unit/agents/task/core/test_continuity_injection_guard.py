"""D6 — once-per-session guard on `_maybe_inject_autonomous_continuity`.

The sibling injectors (`_maybe_inject_episodic_digest`, `_maybe_inject_continuity_bridge`)
skip when `_session_bootstrap_done` is set (the flag is stamped after the session's first
executed step, `agents/task/agent/core/step.py`). The autonomous-continuity injector
lacked that guard, so a self-wake re-entry (`_continue_session=True` resets `n_steps`
back to 1) re-injected the mission-continuity block every turn.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.task.agent.core.memory_prefetch import MemoryPrefetchMixin

SENTINEL_MSG = object()


class _Agent(MemoryPrefetchMixin):
    def __init__(self, *, bootstrap_done: bool):
        self.state = SimpleNamespace(n_steps=1)
        self._is_sub_agent = False
        self._session_bootstrap_done = bootstrap_done
        self.session_id = "sess-guard-test"
        self.user_id = "user_abc"
        self.message_manager = MagicMock()
        self.logger = logging.getLogger("test")


async def _run(agent: _Agent) -> None:
    with (
        patch("agents.task.goals.autonomy_marker.is_autonomous", return_value=True),
        patch("agents.task.agent.core.episodic_digest.build_mission_continuity",
              new=AsyncMock(return_value=SENTINEL_MSG)),
    ):
        await agent._maybe_inject_autonomous_continuity()


@pytest.mark.asyncio
async def test_injects_on_first_bootstrap():
    agent = _Agent(bootstrap_done=False)
    await _run(agent)
    agent.message_manager.push_ephemeral_message.assert_called_once_with(SENTINEL_MSG)


@pytest.mark.asyncio
async def test_skips_after_bootstrap_done():
    """A self-wake re-entry resets n_steps to 1, but the session-bootstrap flag is
    already set — the continuity block must NOT be re-injected."""
    agent = _Agent(bootstrap_done=True)
    await _run(agent)
    agent.message_manager.push_ephemeral_message.assert_not_called()
