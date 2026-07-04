"""Item 8 — memory-prefetch cadence.

`_maybe_prefetch_memory` fires on the first step always. With
`MEMORY_PREFETCH_CADENCE=0` (default) it fires ONLY on the first step (current
behaviour). With cadence=N>0 it additionally fires every N steps. Fail-open: a
missing/None provider message never raises.
"""
import logging

import pytest

from agents.task.agent.core import memory_prefetch as mp


class _State:
    def __init__(self, n_steps):
        self.n_steps = n_steps


class _MM:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _Agent(mp.MemoryPrefetchMixin):
    def __init__(self, n_steps):
        self.state = _State(n_steps)
        self.task = "do something"
        self.session_id = "s1"
        self.message_manager = _MM()
        self.logger = logging.getLogger("test-prefetch")


@pytest.fixture
def _stub_build(monkeypatch):
    """Make build_prefetch_message return a sentinel so a fire => one push."""
    async def _fake(query, *, session_id, user_id=None):
        return object()  # non-None -> mixin pushes it

    monkeypatch.setattr(mp, "build_prefetch_message", _fake)


async def _fires_on(n_steps) -> bool:
    agent = _Agent(n_steps)
    await agent._maybe_prefetch_memory()
    return len(agent.message_manager.pushed) == 1


@pytest.mark.asyncio
async def test_first_step_always_fires(monkeypatch, _stub_build):
    monkeypatch.setattr(mp, "memory_prefetch_cadence", lambda: 0)
    assert await _fires_on(1) is True


@pytest.mark.asyncio
async def test_cadence_zero_is_first_step_only(monkeypatch, _stub_build):
    monkeypatch.setattr(mp, "memory_prefetch_cadence", lambda: 0)
    assert await _fires_on(2) is False
    assert await _fires_on(3) is False


@pytest.mark.asyncio
async def test_cadence_two_fires_on_even_steps(monkeypatch, _stub_build):
    monkeypatch.setattr(mp, "memory_prefetch_cadence", lambda: 2)
    assert await _fires_on(1) is True   # first step always
    assert await _fires_on(2) is True   # 2 % 2 == 0
    assert await _fires_on(3) is False  # 3 % 2 == 1
    assert await _fires_on(4) is True   # 4 % 2 == 0


@pytest.mark.asyncio
async def test_no_provider_message_never_raises(monkeypatch):
    monkeypatch.setattr(mp, "memory_prefetch_cadence", lambda: 2)

    async def _none(query, *, session_id, user_id=None):
        return None

    monkeypatch.setattr(mp, "build_prefetch_message", _none)
    agent = _Agent(2)
    await agent._maybe_prefetch_memory()  # must not raise
    assert agent.message_manager.pushed == []
