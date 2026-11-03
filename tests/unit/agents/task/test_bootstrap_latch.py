"""098 AG-22: a once-per-session injector latches only on success or a decline."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.task.agent.core.memory_prefetch import MemoryPrefetchMixin, bootstrap_latch


class _Agent(MemoryPrefetchMixin):
    def __init__(self):
        self.state = SimpleNamespace(n_steps=1)
        self._is_sub_agent = False
        self.session_id = "s-latch"
        self.user_id = "u1"
        self.orchestrator = None
        self.message_manager = MagicMock()
        self.logger = logging.getLogger("t")


@pytest.mark.asyncio
async def test_bootstrap_latch_not_set_on_error():
    a = _Agent()
    boom = AsyncMock(side_effect=RuntimeError("database is locked"))
    with patch("core.autonomy_marker.is_autonomous", return_value=False), \
         patch("agents.task.agent.core.episodic_digest.build_activity_digest", new=boom):
        await a._maybe_inject_episodic_digest()
    assert not bootstrap_latch(a, "digest").done()
    ok = AsyncMock(return_value="DIGEST")
    with patch("core.autonomy_marker.is_autonomous", return_value=False), \
         patch("agents.task.agent.core.episodic_digest.build_activity_digest", new=ok):
        await a._maybe_inject_episodic_digest()   # the next run's first step retries
        await a._maybe_inject_episodic_digest()   # ... and then never again
    a.message_manager.push_ephemeral_message.assert_called_once_with("DIGEST")
    assert bootstrap_latch(a, "digest").done()


@pytest.mark.asyncio
async def test_bootstrap_latch_set_on_policy_decline():
    a = _Agent()
    with patch("core.autonomy_marker.is_autonomous", return_value=True):
        await a._maybe_inject_episodic_digest()   # autonomous session: declined
    assert bootstrap_latch(a, "digest").done()
    a.message_manager.push_ephemeral_message.assert_not_called()


@pytest.mark.asyncio
async def test_latch_untouched_after_first_step():
    a = _Agent()
    a.state.n_steps = 2
    await a._maybe_inject_episodic_digest()
    assert not bootstrap_latch(a, "digest").done()
