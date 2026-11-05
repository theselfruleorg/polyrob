"""102 M-K: a finished goal or cron session leaves memory soon; a chat keeps its TTL."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.task.task_agent_lifecycle import AUTONOMOUS_SESSION_TTL_S, TaskAgentLifecycleMixin
from core import autonomy_marker


@pytest.mark.asyncio
async def test_only_the_idle_autonomous_session_is_evicted(monkeypatch):
    class _Host(TaskAgentLifecycleMixin):
        pass
    host = _Host.__new__(_Host)
    now = time.time()
    host.session_ttl_seconds = 86400
    host.max_sessions_in_memory = 100
    host._session_last_activity = {
        "goal-old": now - AUTONOMOUS_SESSION_TTL_S - 5,
        "goal-new": now - 5,
        "chat-old": now - AUTONOMOUS_SESSION_TTL_S - 5,
    }
    host._registry = SimpleNamespace(count=lambda: 0, get=lambda sid: None)
    host._cleanup_stale_created_sessions = AsyncMock()
    host._evict_session = AsyncMock()
    autonomy_marker.mark_autonomous("goal-old", "g1")
    autonomy_marker.mark_autonomous("goal-new", "g2")
    await host._cleanup_old_sessions()
    host._evict_session.assert_awaited_once_with("goal-old", reason="autonomous_idle")
