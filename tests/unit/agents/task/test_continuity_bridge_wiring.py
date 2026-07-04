"""Task 6 Part C — first-step continuity-bridge seed
(`MemoryPrefetchMixin._maybe_inject_continuity_bridge`, composed into `Agent` and
invoked from `_prepare_step` alongside the Task-5 activity digest).

Scoped identically to the digest: first-step-only, never for a sub-agent, never for
a session `mark_autonomous` has tagged. The session's `thread_key` is resolved by a
reverse lookup on `session_chat_registry` (`resolve_by_session_id`) via the agent's
container — mirroring the existing reverse-lookup pattern in
`agents/task_agent_lite.py::_rebind_recreated_chat`.
"""
import logging

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from modules.memory.episodic import finalize_episode
from agents.task.agent.core import memory_prefetch as mp
from agents.task.goals.autonomy_marker import mark_autonomous


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    monkeypatch.setenv("CONTINUITY_BRIDGE_ENABLED", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


class _State:
    def __init__(self, n_steps):
        self.n_steps = n_steps


class _MM:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _FakeChatRegistry:
    def __init__(self, rows):
        self._rows = rows  # session_id -> row dict

    def resolve_by_session_id(self, session_id):
        return self._rows.get(session_id)


class _FakeContainer:
    def __init__(self, registry=None):
        self._svc = {"session_chat_registry": registry} if registry else {}

    def get_service(self, name):
        return self._svc.get(name)


class _Agent(mp.MemoryPrefetchMixin):
    def __init__(self, n_steps, session_id, container=None, is_sub_agent=False):
        self.state = _State(n_steps)
        self.task = "do something"
        self.session_id = session_id
        self.user_id = "u1"
        self._is_sub_agent = is_sub_agent
        self.container = container
        self.message_manager = _MM()
        self.logger = logging.getLogger("test-continuity-bridge-wiring")


@pytest.mark.asyncio
async def test_bridge_seeds_for_chat_session_with_prior_episode(provider):
    await finalize_episode(session_id="old-1", user_id="u1", kind="chat",
                           thread_key="tg:42", outcome="done",
                           summary="We drafted the launch tweet.")
    registry = _FakeChatRegistry({"chat-session-1": {"session_key": "tg:42"}})
    agent = _Agent(n_steps=1, session_id="chat-session-1",
                   container=_FakeContainer(registry))

    await agent._maybe_inject_continuity_bridge()

    assert len(agent.message_manager.pushed) == 1
    msg = agent.message_manager.pushed[0]
    from modules.llm.messages import MessageOrigin
    assert getattr(msg, "origin", None) == MessageOrigin.SESSION_BRIDGE
    assert "launch tweet" in msg.content


@pytest.mark.asyncio
async def test_bridge_does_not_seed_without_prior_episode(provider):
    registry = _FakeChatRegistry({"chat-session-2": {"session_key": "tg:99"}})
    agent = _Agent(n_steps=1, session_id="chat-session-2",
                   container=_FakeContainer(registry))

    await agent._maybe_inject_continuity_bridge()

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_bridge_does_not_seed_for_autonomous_session(provider):
    await finalize_episode(session_id="old-2", user_id="u1", kind="chat",
                           thread_key="tg:7", outcome="done", summary="prior context")
    autonomous_session_id = "goal-session-marked-autonomous"
    mark_autonomous(autonomous_session_id)
    registry = _FakeChatRegistry({autonomous_session_id: {"session_key": "tg:7"}})
    agent = _Agent(n_steps=1, session_id=autonomous_session_id,
                   container=_FakeContainer(registry))

    await agent._maybe_inject_continuity_bridge()

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_bridge_does_not_seed_for_subagent(provider):
    await finalize_episode(session_id="old-3", user_id="u1", kind="chat",
                           thread_key="tg:8", outcome="done", summary="prior context")
    registry = _FakeChatRegistry({"chat-session-3": {"session_key": "tg:8"}})
    agent = _Agent(n_steps=1, session_id="chat-session-3",
                   container=_FakeContainer(registry), is_sub_agent=True)

    await agent._maybe_inject_continuity_bridge()

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_bridge_only_fires_on_first_step(provider):
    await finalize_episode(session_id="old-4", user_id="u1", kind="chat",
                           thread_key="tg:10", outcome="done", summary="prior context")
    registry = _FakeChatRegistry({"chat-session-4": {"session_key": "tg:10"}})
    agent = _Agent(n_steps=2, session_id="chat-session-4",
                   container=_FakeContainer(registry))

    await agent._maybe_inject_continuity_bridge()

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_bridge_no_registry_returns_none_safely(provider):
    agent = _Agent(n_steps=1, session_id="chat-session-5", container=None)

    await agent._maybe_inject_continuity_bridge()  # must not raise

    assert agent.message_manager.pushed == []


@pytest.mark.asyncio
async def test_bridge_never_raises_on_registry_error(provider, monkeypatch):
    class _BoomContainer:
        def get_service(self, name):
            raise RuntimeError("container down")

    agent = _Agent(n_steps=1, session_id="chat-session-6", container=_BoomContainer())

    await agent._maybe_inject_continuity_bridge()  # must not raise

    assert agent.message_manager.pushed == []
