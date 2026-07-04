"""Regression guard: TaskAgent._chat_locks must not grow forever.

A lock is created per unique (user_id, chat_id) pair the first time chat_once()
is called for it, and previously was never removed even after the associated
chat session was dropped. This only matters at meaningful scale (many distinct
chat keys on a long-running server), but it is a real unbounded dict with no
eviction path, so it gets a real fix and a real regression test.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_task_agent():
    from agents.task_agent_lite import TaskAgent
    agent = TaskAgent.__new__(TaskAgent)  # bypass heavy __init__
    agent._initialized = True
    agent.task_available = True
    agent._chat_sessions = {}
    agent._chat_locks = {}
    agent._registry = MagicMock()
    agent.session_manager = MagicMock()
    return agent


def test_lock_evicted_when_session_creation_fails(monkeypatch):
    """If _chat_once_locked raises before re-populating _chat_sessions, the
    lock for that key must not be left behind forever."""
    from agents.task_agent_lite import TaskAgent

    agent = _make_task_agent()

    async def _boom(self, user_id, text, key, provider=None, model=None):
        raise RuntimeError("session creation failed")

    monkeypatch.setattr(TaskAgent, "_chat_once_locked", _boom)

    async def run():
        with pytest.raises(RuntimeError):
            await agent.chat_once("user-1", "hello", chat_id="chat-1")

    asyncio.run(run())
    key = agent._chat_key("user-1", "chat-1")
    assert key not in agent._chat_locks


def test_lock_kept_when_session_succeeds(monkeypatch):
    """The common path: a successful turn re-populates _chat_sessions, so the
    lock for that key must be kept (not evicted -- it protects the next turn
    for the same chat)."""
    from agents.task_agent_lite import TaskAgent

    agent = _make_task_agent()

    async def _ok(self, user_id, text, key, provider=None, model=None):
        self._chat_sessions[key] = "session-123"
        return "reply"

    monkeypatch.setattr(TaskAgent, "_chat_once_locked", _ok)

    async def run():
        return await agent.chat_once("user-1", "hello", chat_id="chat-1")

    result = asyncio.run(run())
    assert result == "reply"
    key = agent._chat_key("user-1", "chat-1")
    assert key in agent._chat_locks
