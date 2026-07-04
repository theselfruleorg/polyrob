"""Task 5 — session-start activity digest (chat/owner sessions only).

Two layers under test:

1. The pure builder `build_activity_digest` (Steps 1-3 of the task brief): built for
   chat sessions with in-window episodes, skipped for goal/subagent/empty/off.
2. The first-step wiring (`MemoryPrefetchMixin._maybe_inject_episodic_digest`,
   composed into `Agent` and invoked from `_prepare_step`): fires exactly once, on
   step 1, for a chat/owner session, and NEVER fires when the session has been
   marked autonomous (`agents.task.goals.autonomy_marker.mark_autonomous`) — this is
   the reliability property the brief calls out: `mark_autonomous(session_id)` runs
   AFTER `create_session()` but BEFORE `run_session()`, so Agent CONSTRUCTION time is
   too early to read the marker reliably; the first step of the run loop (this test)
   is the earliest point where it is guaranteed to be set for an autonomous session.
"""
import logging
import time

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from agents.task.agent.core.episodic_digest import build_activity_digest  # new (Step 3)
from agents.task.agent.core import memory_prefetch as mp
from agents.task.goals.autonomy_marker import mark_autonomous


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    monkeypatch.setenv("EPISODIC_DIGEST_INJECT", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


@pytest.mark.asyncio
async def test_digest_built_for_chat(provider):
    from modules.memory.episodic import finalize_episode
    for i in range(3):
        await finalize_episode(session_id=f"g{i}", user_id="u1", kind="goal",
                               task=f"task {i}", outcome="done", spend_usd=1.0)
    msg = await build_activity_digest(user_id="u1", kind="chat", is_sub_agent=False)
    assert msg is not None
    assert "3 runs" in msg.content and "3.00" in msg.content
    assert "<untrusted_tool_result" in msg.content


@pytest.mark.asyncio
async def test_digest_skipped_for_goal_session(provider):
    msg = await build_activity_digest(user_id="u1", kind="goal", is_sub_agent=False)
    assert msg is None


@pytest.mark.asyncio
async def test_digest_skipped_for_subagent(provider):
    msg = await build_activity_digest(user_id="u1", kind="chat", is_sub_agent=True)
    assert msg is None


@pytest.mark.asyncio
async def test_digest_empty_history_returns_none(provider):
    msg = await build_activity_digest(user_id="u1", kind="chat", is_sub_agent=False)
    assert msg is None


@pytest.mark.asyncio
async def test_digest_off_returns_none(provider, monkeypatch):
    monkeypatch.setenv("EPISODIC_DIGEST_INJECT", "false")
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g", user_id="u1", kind="goal", outcome="done")
    msg = await build_activity_digest(user_id="u1", kind="chat", is_sub_agent=False)
    assert msg is None


# ---------------------------------------------------------------------------
# Wiring: MemoryPrefetchMixin._maybe_inject_episodic_digest, first-step-only,
# chat-only, autonomous-aware.
# ---------------------------------------------------------------------------

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
        self.task = "do something"
        self.session_id = session_id
        self.user_id = "u1"
        self._is_sub_agent = is_sub_agent
        self.message_manager = _MM()
        self.logger = logging.getLogger("test-episodic-digest-wiring")


@pytest.mark.asyncio
async def test_wiring_fires_for_chat_session_first_step(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g1", user_id="u1", kind="goal",
                           task="did a thing", outcome="done", spend_usd=2.0)

    agent = _Agent(n_steps=1, session_id="chat-session-1")
    await agent._maybe_inject_episodic_digest()

    assert len(agent.message_manager.pushed) == 1
    msg = agent.message_manager.pushed[0]
    from modules.llm.messages import MessageOrigin
    assert getattr(msg, "origin", None) == MessageOrigin.EPISODIC_DIGEST


@pytest.mark.asyncio
async def test_wiring_does_not_fire_for_autonomous_session(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g2", user_id="u1", kind="goal",
                           task="did a thing", outcome="done", spend_usd=2.0)

    autonomous_session_id = "goal-session-marked-autonomous"
    mark_autonomous(autonomous_session_id)

    agent = _Agent(n_steps=1, session_id=autonomous_session_id)
    await agent._maybe_inject_episodic_digest()

    # Must NOT inject an EPISODIC_DIGEST message into a session the dispatcher has
    # marked autonomous — this is the key correctness property for Task 5.
    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_wiring_does_not_fire_for_subagent(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g3", user_id="u1", kind="goal",
                           task="did a thing", outcome="done", spend_usd=2.0)

    agent = _Agent(n_steps=1, session_id="chat-session-2", is_sub_agent=True)
    await agent._maybe_inject_episodic_digest()

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_wiring_only_fires_on_first_step(provider):
    from modules.memory.episodic import finalize_episode
    await finalize_episode(session_id="g4", user_id="u1", kind="goal",
                           task="did a thing", outcome="done", spend_usd=2.0)

    agent = _Agent(n_steps=2, session_id="chat-session-3")
    await agent._maybe_inject_episodic_digest()

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_wiring_never_raises_on_provider_error(provider, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "agents.task.agent.core.episodic_digest.build_activity_digest", _boom
    )
    agent = _Agent(n_steps=1, session_id="chat-session-4")
    await agent._maybe_inject_episodic_digest()  # must not raise
    assert agent.message_manager.pushed == []
