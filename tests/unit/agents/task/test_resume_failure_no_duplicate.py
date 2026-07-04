"""Resume-failure should not silently spawn a duplicate session (B2).

When a user's prior session can't be resumed (its live orchestrator isn't in memory
AND _recreate_orchestrator returns None), process_user_message used to fall through
and silently create a brand-new empty session while telling the user 'Task started'
— losing conversation continuity invisibly and leaking the old session toward the
per-user limit.

Fix: fresh-start-with-notice — still create a new session (so the user is never stuck),
but (a) tell the user the previous session couldn't be resumed, and (b) retire the
un-recreatable session from in-memory tracking (history kept on disk, delete_files=False)
so it stops leaking the limit and isn't re-selected next message.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.agent.session import SessionManager
from agents.task_agent_lite import TaskAgent
from core.config import BotConfig
from core.container import DependencyContainer


def _make_agent(tmp_path):
    config = BotConfig()
    container = DependencyContainer.get_instance(config)
    agent = TaskAgent(config=config, container=container)
    agent.session_manager = SessionManager(base_dir=str(tmp_path))
    agent.task_available = True
    agent._initialized = True
    return agent


@pytest.mark.asyncio
async def test_resume_failure_notifies_and_does_not_leak_old_session(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path)
    sm = agent.session_manager

    old_id = sm.create_session("old-session", user_id="u1")
    sm.update_session_status(old_id, "suspended")
    agent.user_sessions["u1"] = old_id

    # Live orchestrator absent + recreation fails → the resume-failure branch.
    monkeypatch.setattr(agent._registry, "get", lambda sid: None)
    monkeypatch.setattr(agent, "_recreate_orchestrator", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "create_session", AsyncMock(return_value={"id": "new-session"}))
    monkeypatch.setattr(agent, "run_session", AsyncMock())

    result = await agent.process_user_message("u1", "continue please")

    # New session is created (user never stuck)...
    agent.create_session.assert_awaited_once()
    assert "new-session" in result
    # ...but the user is TOLD the previous one couldn't be resumed (not a silent success).
    assert "resume" in result.lower()
    # ...and the un-recreatable old session is retired from in-memory tracking,
    assert old_id not in sm._sessions
    assert sm.get_active_sessions("u1") == ["new-session"] or "new-session" not in sm._sessions
    # ...so it no longer leaks the per-user limit via the old id.
    assert old_id not in sm.get_active_sessions("u1")


@pytest.mark.asyncio
async def test_successful_resume_is_unchanged(tmp_path, monkeypatch):
    """Regression guard: when recreation succeeds, we resume the existing session and
    do NOT create a new one."""
    agent = _make_agent(tmp_path)
    sm = agent.session_manager

    old_id = sm.create_session("old-session", user_id="u1")
    sm.update_session_status(old_id, "suspended")
    agent.user_sessions["u1"] = old_id

    orch = MagicMock()
    orch.submit_user_message = AsyncMock()
    monkeypatch.setattr(agent._registry, "get", lambda sid: None)
    monkeypatch.setattr(agent, "_recreate_orchestrator", AsyncMock(return_value=orch))
    monkeypatch.setattr(agent, "create_session", AsyncMock())
    monkeypatch.setattr(agent, "run_session", AsyncMock())

    result = await agent.process_user_message("u1", "continue please")

    orch.submit_user_message.assert_awaited_once()
    agent.create_session.assert_not_awaited()
    assert old_id in result
    assert old_id in sm._sessions  # still tracked
