"""H16 / C1: an injector that carries owner-tenant state (memory recall, the
episodic digest, both continuity bridges, the live-health note) never fires in
a correspondent-facing session — one created for a correspondent or tainted by
one. Its output goes to a third party. Mirrors owner_thread_inject's guard."""
import asyncio
import logging
import types
from unittest.mock import AsyncMock

import pytest

from agents.task.agent.core import live_health, memory_prefetch


class _MM:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _Container:
    def get_service(self, name):
        if name == "session_chat_registry":
            return types.SimpleNamespace(
                resolve_by_session_id=lambda sid: {"session_key": "tg:1"})
        return None


class _Agent(memory_prefetch.MemoryPrefetchMixin, live_health.LiveHealthMixin):
    def __init__(self, *, created=False, tainted=False):
        self.orchestrator = types.SimpleNamespace(
            _public_session=False, _correspondent_session=created,
            _correspondent_tainted=tainted, container=None, task_agent=None)
        self.container = _Container()
        self.state = types.SimpleNamespace(n_steps=1)
        self._is_sub_agent = False
        self._session_bootstrap_done = False
        self.session_id = "s"
        self.user_id = "u"
        self.task = "t"
        self.logger = logging.getLogger("test.correspondent_facing")
        self.message_manager = _MM()


@pytest.fixture
def _stubs(monkeypatch):
    import agents.task.agent.core.episodic_digest as ed
    import core.surfaces.continuity as cont
    sentinel = types.SimpleNamespace(content="SENTINEL")
    monkeypatch.setattr(memory_prefetch, "build_prefetch_message",
                        AsyncMock(return_value=sentinel))
    monkeypatch.setattr(ed, "build_activity_digest", AsyncMock(return_value=sentinel))
    monkeypatch.setattr(ed, "build_mission_continuity", AsyncMock(return_value=sentinel))
    monkeypatch.setattr(cont, "build_bridge_message", AsyncMock(return_value=sentinel))
    monkeypatch.setattr(live_health, "build_live_health_text", lambda *a, **k: "HEALTH")
    monkeypatch.delenv("LIVE_HEALTH_CONTEXT", raising=False)
    from agents.task.constants import AutonomyConfig
    monkeypatch.setattr(AutonomyConfig, "continuity_bridge_enabled",
                        staticmethod(lambda: True))
    return sentinel


_CHAT_INJECTORS = [
    memory_prefetch.MemoryPrefetchMixin._maybe_prefetch_memory,
    memory_prefetch.MemoryPrefetchMixin._maybe_inject_episodic_digest,
    memory_prefetch.MemoryPrefetchMixin._maybe_inject_continuity_bridge,
    live_health.LiveHealthMixin._maybe_inject_live_health,
]


@pytest.mark.parametrize("fn", _CHAT_INJECTORS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("kw", [{"created": True}, {"tainted": True}],
                         ids=["created", "tainted"])
def test_chat_injectors_skip_correspondent_facing(_stubs, monkeypatch, fn, kw):
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda _sid: False)
    agent = _Agent(**kw)
    asyncio.run(fn(agent))
    assert agent.message_manager.pushed == []


@pytest.mark.parametrize("fn", _CHAT_INJECTORS, ids=lambda f: f.__name__)
def test_chat_injectors_fire_for_owner(_stubs, monkeypatch, fn):
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda _sid: False)
    agent = _Agent()
    asyncio.run(fn(agent))
    assert len(agent.message_manager.pushed) == 1


def test_autonomous_continuity_skips_correspondent_facing(_stubs, monkeypatch):
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda _sid: True)
    fn = memory_prefetch.MemoryPrefetchMixin._maybe_inject_autonomous_continuity
    tainted = _Agent(tainted=True)
    asyncio.run(fn(tainted))
    assert tainted.message_manager.pushed == []
    owner = _Agent()
    asyncio.run(fn(owner))
    assert owner.message_manager.pushed == [_stubs]
