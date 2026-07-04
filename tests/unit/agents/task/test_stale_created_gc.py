"""GC for never-run 'created' sessions (the stale-created limit-exhaustion bug).

A session stuck in 'created' counts toward the per-user limit (get_active_sessions),
but the TTL/LRU GC keys off _session_last_activity, which is only written on the run
path. So a created-but-never-run session is invisible to that GC and consumes the
per-user slot forever, eventually blocking all new runs. These tests pin the sweep
that retires such sessions (keyed off 'created_at', which is always set).
"""
import datetime

import pytest

from agents.task.agent.session import SessionManager
from agents.task_agent_lite import TaskAgent
from core.config import BotConfig
from core.container import DependencyContainer


def _make_agent(tmp_path):
    """Construct a TaskAgent (sync __init__) with a real, isolated SessionManager."""
    config = BotConfig()
    container = DependencyContainer.get_instance(config)
    agent = TaskAgent(config=config, container=container)
    agent.session_manager = SessionManager(base_dir=str(tmp_path))
    return agent


def _backdate(session_manager, session_id, *, seconds_old):
    """Force a session's created_at into the past for deterministic age."""
    old = datetime.datetime.now() - datetime.timedelta(seconds=seconds_old)
    session_manager._sessions[session_id]["created_at"] = old.isoformat()


@pytest.mark.asyncio
async def test_stale_created_session_is_retired_and_frees_limit(tmp_path):
    agent = _make_agent(tmp_path)
    agent.created_session_ttl_seconds = 1800  # 30 min

    sm = agent.session_manager
    for i in range(3):
        sid = sm.create_session(f"sess-{i}", user_id="u1")
        _backdate(sm, sid, seconds_old=3600)  # 1h old, never ran

    assert len(sm.get_active_sessions("u1")) == 3  # all count toward the limit

    retired = await agent._cleanup_stale_created_sessions()

    assert retired == 3
    assert sm.get_active_sessions("u1") == []  # limit slot freed
    assert sm._sessions == {}  # in-memory entry gone


@pytest.mark.asyncio
async def test_recent_created_session_is_preserved(tmp_path):
    agent = _make_agent(tmp_path)
    agent.created_session_ttl_seconds = 1800

    sm = agent.session_manager
    sid = sm.create_session("fresh", user_id="u1")
    _backdate(sm, sid, seconds_old=60)  # only 1 min old — not stale

    retired = await agent._cleanup_stale_created_sessions()

    assert retired == 0
    assert sm.get_active_sessions("u1") == [sid]  # still tracked


@pytest.mark.asyncio
async def test_created_session_with_activity_is_not_swept(tmp_path):
    """A 'created' session that has a run-path activity timestamp is the normal
    in-flight case and must not be deleted by this sweep."""
    agent = _make_agent(tmp_path)
    agent.created_session_ttl_seconds = 1800

    sm = agent.session_manager
    sid = sm.create_session("running-soon", user_id="u1")
    _backdate(sm, sid, seconds_old=3600)  # old...
    agent._session_last_activity[sid] = 0.0  # ...but it has activity → run path owns it

    retired = await agent._cleanup_stale_created_sessions()

    assert retired == 0
    assert sm.get_active_sessions("u1") == [sid]
