"""Task 6 Part A — cleanup writes an H-MEM-derived summary onto the closing chat
episode, gated on ``AutonomyConfig.continuity_bridge_enabled()``.

`SessionCleanupMixin.cleanup()`'s existing chat-episode `finalize_episode` call
(added by an earlier task) previously never passed a `summary`. This adds
`_derive_closing_chat_summary` (best-effort, fail-open, reached via
`agent.task_context_manager.get_session(session_id).context_retriever
._format_session_summary()`) and wires it in ONLY when the bridge flag is on, so the
flag-off path stays byte-identical (summary stays None).
"""
from unittest.mock import MagicMock

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
import modules.memory.registry as reg
from agents.task.session.cleanup import SessionCleanupMixin, _derive_closing_chat_summary
from core.surfaces.continuity import build_bridge_message


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


class _FakeContextRetriever:
    def __init__(self, summary_text):
        self._summary_text = summary_text

    def _format_session_summary(self):
        return self._summary_text


class _FakeSessionData:
    def __init__(self, context_retriever):
        self.context_retriever = context_retriever


class _FakeTaskContextManager:
    def __init__(self, sessions):
        self._sessions = sessions

    def get_session(self, session_id):
        return self._sessions.get(session_id)


class _FakeAgent:
    def __init__(self, task_context_manager=None, is_sub_agent=False):
        self.task_context_manager = task_context_manager
        self._is_sub_agent = is_sub_agent


class _MinimalOrchestrator(SessionCleanupMixin):
    """Smallest object satisfying cleanup()'s attribute probes (mirrors the pattern
    in test_session_cleanup_browser_configs.py)."""

    def __init__(self, session_id, user_id, agents, chat_session_key=None):
        self.session_id = session_id
        self.user_id = user_id
        self.agents = agents
        self._chat_session_key = chat_session_key
        self._browser_contexts = set()
        self.browser_manager = None
        self.controller = None
        self.session_manager = None
        self.logger = MagicMock()


@pytest.mark.asyncio
async def test_cleanup_attaches_hmem_summary_when_bridge_enabled(provider):
    retriever = _FakeContextRetriever(
        "[HIERARCHICAL MEMORY - SESSION CONTEXT]\n\n"
        "Session: s1\nTask: draft the launch tweet\nProgress: 3/5\n"
        "Current Phase: drafting"
    )
    tcm = _FakeTaskContextManager({"cleanup-summary-sess-1": _FakeSessionData(retriever)})
    orch = _MinimalOrchestrator("cleanup-summary-sess-1", "u1", {"main": _FakeAgent(tcm)},
                                chat_session_key="tg:42")

    await orch.cleanup(full_cleanup=True, status="completed")

    # Consumed exactly the way the bridge consumes it (Part B/C).
    msg = await build_bridge_message(user_id="u1", thread_key="tg:42")
    assert msg is not None
    assert "launch tweet" in msg.content


@pytest.mark.asyncio
async def test_cleanup_summary_stays_none_when_bridge_disabled(provider, monkeypatch):
    monkeypatch.setenv("CONTINUITY_BRIDGE_ENABLED", "false")
    retriever = _FakeContextRetriever("Task: draft the launch tweet")
    tcm = _FakeTaskContextManager({"cleanup-summary-sess-2": _FakeSessionData(retriever)})
    orch = _MinimalOrchestrator("cleanup-summary-sess-2", "u1", {"main": _FakeAgent(tcm)},
                                chat_session_key="tg:43")

    await orch.cleanup(full_cleanup=True, status="completed")

    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key="tg:43", limit=1)
    assert out and (out[0].summary or "") == ""


@pytest.mark.asyncio
async def test_cleanup_failopen_when_no_task_context_manager(provider):
    orch = _MinimalOrchestrator("cleanup-summary-sess-3", "u1", {"main": _FakeAgent(None)},
                                chat_session_key="tg:44")

    await orch.cleanup(full_cleanup=True, status="completed")  # must not raise

    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key="tg:44", limit=1)
    assert out and (out[0].summary or "") == ""


@pytest.mark.asyncio
async def test_cleanup_failopen_when_no_agents(provider):
    orch = _MinimalOrchestrator("cleanup-summary-sess-4", "u1", {}, chat_session_key="tg:45")

    await orch.cleanup(full_cleanup=True, status="completed")  # must not raise

    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key="tg:45", limit=1)
    assert out and (out[0].summary or "") == ""


def test_derive_closing_chat_summary_skips_sub_agents():
    retriever = _FakeContextRetriever("should not be picked")
    tcm = _FakeTaskContextManager({"cleanup-summary-sess-5": _FakeSessionData(retriever)})
    sub_agent = _FakeAgent(tcm, is_sub_agent=True)
    orch = _MinimalOrchestrator("cleanup-summary-sess-5", "u1", {"sub": sub_agent})

    assert _derive_closing_chat_summary(orch) is None


def test_derive_closing_chat_summary_caps_length():
    long_text = "x" * 900
    retriever = _FakeContextRetriever(long_text)
    tcm = _FakeTaskContextManager({"cleanup-summary-sess-6": _FakeSessionData(retriever)})
    orch = _MinimalOrchestrator("cleanup-summary-sess-6", "u1", {"main": _FakeAgent(tcm)})

    summary = _derive_closing_chat_summary(orch)
    assert summary is not None and len(summary) <= 500
