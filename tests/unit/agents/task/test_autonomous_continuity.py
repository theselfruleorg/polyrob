"""Autonomous continuity bridge (§7.5) — carry recent activity INTO an autonomous
goal/cron tick so it stops re-deriving 'nothing new' each time.

Mirror-image of the chat digest: fires ONLY for autonomous sessions (never chat),
gated AUTONOMOUS_CONTINUITY_BRIDGE, first-step-only, never sub-agent.
"""
import logging

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from agents.task.agent.core.episodic_digest import build_mission_continuity
from agents.task.agent.core import memory_prefetch as mp
from agents.task.goals.autonomy_marker import mark_autonomous


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    monkeypatch.setenv("AUTONOMOUS_CONTINUITY_BRIDGE", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_continuity_built_from_recent_episodes(provider):
    from modules.memory.episodic import finalize_episode
    for i in range(2):
        await finalize_episode(session_id=f"g{i}", user_id="u1", kind="goal",
                               task=f"posted thread {i}", outcome="done", spend_usd=0.5)
    msg = await build_mission_continuity(user_id="u1")
    assert msg is not None
    assert "posted thread" in msg.content


@pytest.mark.asyncio
async def test_continuity_none_when_flag_off(provider, monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_CONTINUITY_BRIDGE", "false")
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g", user_id="u1", kind="goal", outcome="done")
    assert await build_mission_continuity(user_id="u1") is None


@pytest.mark.asyncio
async def test_continuity_none_when_empty(provider):
    assert await build_mission_continuity(user_id="u1") is None


# --- wiring ------------------------------------------------------------------

class _State:
    def __init__(self, n_steps):
        self.n_steps = n_steps


class _MM:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _Agent(mp.MemoryPrefetchMixin):
    def __init__(self, n_steps, session_id, is_sub_agent=False):
        self.state = _State(n_steps)
        self.session_id = session_id
        self.user_id = "u1"
        self._is_sub_agent = is_sub_agent
        self.message_manager = _MM()
        self.logger = logging.getLogger("test-autonomous-continuity")


@pytest.mark.asyncio
async def test_wiring_fires_for_autonomous_first_step(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g1", user_id="u1", kind="goal",
                           task="did a thing", outcome="done", spend_usd=1.0)
    sid = "goal-session-A"
    mark_autonomous(sid)
    agent = _Agent(n_steps=1, session_id=sid)
    await agent._maybe_inject_autonomous_continuity()
    assert len(agent.message_manager.pushed) == 1


@pytest.mark.asyncio
async def test_wiring_does_not_fire_for_chat(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g2", user_id="u1", kind="goal",
                           task="did a thing", outcome="done", spend_usd=1.0)
    # a non-autonomous (chat) session must NOT get the autonomous bridge
    agent = _Agent(n_steps=1, session_id="chat-session-A")
    await agent._maybe_inject_autonomous_continuity()
    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_wiring_first_step_only(provider):
    sid = "goal-session-B"
    mark_autonomous(sid)
    agent = _Agent(n_steps=2, session_id=sid)
    await agent._maybe_inject_autonomous_continuity()
    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_wiring_never_raises(provider, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(
        "agents.task.agent.core.episodic_digest.build_mission_continuity", _boom)
    sid = "goal-session-C"
    mark_autonomous(sid)
    agent = _Agent(n_steps=1, session_id=sid)
    await agent._maybe_inject_autonomous_continuity()  # must not raise
    assert agent.message_manager.pushed == []
