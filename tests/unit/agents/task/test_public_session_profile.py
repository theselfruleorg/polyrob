"""044 T4 ratchet: a PUBLIC session pins no owner/memory/health/project origin.

Source-level pins (cheap, no orchestrator build): every injector that carries
owner-tenant state must consult is_public_session() and return early."""
import asyncio
import inspect
import logging
import types
from unittest.mock import AsyncMock

from agents.task.agent.core import construction, live_health, memory_prefetch


def _src(fn):
    return inspect.getsource(fn)


def test_self_context_block_skips_owner_docs_in_public_sessions():
    src = inspect.getsource(construction)
    i = src.index("load_owner_doc(_data_dir")
    assert src.rfind("is_public_session(", 0, i) != -1


def test_project_context_skips_public_sessions():
    assert "is_public_session(" in _src(construction.AgentConstructionMixin._load_project_context)


def test_memory_injectors_skip_public_sessions():
    assert "is_public_session(" in _src(memory_prefetch.MemoryPrefetchMixin._maybe_prefetch_memory)
    assert "is_public_session(" in _src(memory_prefetch.MemoryPrefetchMixin._maybe_inject_episodic_digest)


def test_live_health_skips_public_sessions():
    assert "is_public_session(" in _src(live_health.LiveHealthMixin._maybe_inject_live_health)


# --- Fix round 1 (review Important #2): behavioural coverage -----------------
# The source-inspection tests above only prove the guard TEXT is present, not that
# it actually short-circuits at runtime. These build a lightweight real double
# (mixes in the two mixins under test so their own helper methods, e.g.
# _build_recall_query, are genuinely bound -- not stubbed) and run the real
# method bodies end-to-end, asserting the downstream push never fires when public
# and DOES fire when private (so a vacuous "public path never runs" test can't
# pass for the wrong reason -- see the monkeypatched-builder half of each test).


class _FakeMessageManager:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _FakeAgent(memory_prefetch.MemoryPrefetchMixin, live_health.LiveHealthMixin):
    """Minimal behavioural double: just enough state for the real injector
    bodies (inherited, not stubbed) to run to completion."""

    def __init__(self, *, public: bool):
        self.orchestrator = types.SimpleNamespace(
            _public_session=public, container=None, task_agent=None)
        self.state = types.SimpleNamespace(n_steps=1)
        self._is_sub_agent = False
        self._session_bootstrap_done = False
        self.session_id = "s"
        self.user_id = "u"
        self.task = "t"
        self.logger = logging.getLogger("test.public_session_profile")
        self.message_manager = _FakeMessageManager()


def test_prefetch_memory_behavioural_public_vs_private(monkeypatch):
    sentinel = types.SimpleNamespace(content="SENTINEL_RECALL")
    monkeypatch.setattr(memory_prefetch, "build_prefetch_message",
                        AsyncMock(return_value=sentinel))

    public_agent = _FakeAgent(public=True)
    asyncio.run(memory_prefetch.MemoryPrefetchMixin._maybe_prefetch_memory(public_agent))
    assert public_agent.message_manager.pushed == []

    private_agent = _FakeAgent(public=False)
    asyncio.run(memory_prefetch.MemoryPrefetchMixin._maybe_prefetch_memory(private_agent))
    assert private_agent.message_manager.pushed == [sentinel]


def test_episodic_digest_behavioural_public_vs_private(monkeypatch):
    sentinel = types.SimpleNamespace(content="SENTINEL_DIGEST")
    import agents.task.agent.core.episodic_digest as episodic_digest
    monkeypatch.setattr(episodic_digest, "build_activity_digest",
                        AsyncMock(return_value=sentinel))

    public_agent = _FakeAgent(public=True)
    asyncio.run(memory_prefetch.MemoryPrefetchMixin._maybe_inject_episodic_digest(public_agent))
    assert public_agent.message_manager.pushed == []

    private_agent = _FakeAgent(public=False)
    asyncio.run(memory_prefetch.MemoryPrefetchMixin._maybe_inject_episodic_digest(private_agent))
    assert private_agent.message_manager.pushed == [sentinel]


def test_live_health_behavioural_public_vs_private(monkeypatch):
    monkeypatch.delenv("LIVE_HEALTH_CONTEXT", raising=False)
    monkeypatch.setattr(live_health, "build_live_health_text",
                        lambda *a, **k: "SENTINEL_HEALTH_TEXT")

    public_agent = _FakeAgent(public=True)
    asyncio.run(live_health.LiveHealthMixin._maybe_inject_live_health(public_agent))
    assert public_agent.message_manager.pushed == []

    private_agent = _FakeAgent(public=False)
    asyncio.run(live_health.LiveHealthMixin._maybe_inject_live_health(private_agent))
    assert len(private_agent.message_manager.pushed) == 1
    assert "SENTINEL_HEALTH_TEXT" in private_agent.message_manager.pushed[0].content


# --- 044 I3 / I5 (final review) -------------------------------------------
# Two injectors had NO public guard: the autonomous continuity bridge (which a
# room's SERVICE run reaches, because that run IS an autonomous goal run bound
# to the room's session) and the <environment> block (host, workspace path,
# posture axes, host executables).


def test_autonomous_continuity_skips_public_sessions():
    assert "is_public_session(" in _src(
        memory_prefetch.MemoryPrefetchMixin._maybe_inject_autonomous_continuity)


def test_autonomous_continuity_behavioural_public_vs_private(monkeypatch):
    sentinel = types.SimpleNamespace(content="SENTINEL_CONTINUITY")
    import agents.task.agent.core.episodic_digest as episodic_digest
    monkeypatch.setattr(episodic_digest, "build_mission_continuity",
                        AsyncMock(return_value=sentinel))
    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous",
                        lambda _sid: True)

    public_agent = _FakeAgent(public=True)
    asyncio.run(memory_prefetch.MemoryPrefetchMixin
                ._maybe_inject_autonomous_continuity(public_agent))
    assert public_agent.message_manager.pushed == []

    private_agent = _FakeAgent(public=False)
    asyncio.run(memory_prefetch.MemoryPrefetchMixin
                ._maybe_inject_autonomous_continuity(private_agent))
    assert private_agent.message_manager.pushed == [sentinel]


def test_environment_block_skips_public_sessions():
    src = inspect.getsource(construction)
    i = src.index("set_environment_message(_env_block)")
    window = src[max(0, i - 1200):i]
    assert "is_public_session(" in window
