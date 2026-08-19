"""Restore path must not pass the flat legacy session_info['config'] shape as
TaskSessionConfig — it always fails validation.

Live evidence (2026-08-17 15:53:04Z, session fad0f70e, a self-wake restore):
`WARNING task.executor: Failed to apply session config: 5 validation errors for
TaskSessionConfig` on model/provider/max_steps/temperature/use_vision. Root cause:
`_recreate_orchestrator` (agents/task_agent_lite.py) fell back to
`session_info['config']` — a FLAT dict `{model, provider, tools, max_steps,
temperature, use_vision, tools_config}` written for API-compat elsewhere in this
file — as the `session_config=` kwarg to `create_agent`. TaskSessionConfig expects
the nested shape (`llm={...}`, `limits={...}`) and has `extra='forbid'`, so this
fallback branch failed 100% of the time it was exercised, not just once; the
exception was silently swallowed to a warning in construction.py and model/
provider/use_vision were still correctly applied via other explicit kwargs, so
impact was low — but the warning was pure noise on every self-wake restore.

Fix: only pass an explicit request-supplied session_config (already correctly
shaped); drop the flat-config fallback since it can never validate and nothing
it carries is otherwise lost (model/provider/use_vision are applied via the
`llm`/`use_vision` kwargs at the same call site).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.config import TaskSessionConfig
from agents.task.agent.session import SessionManager
from agents.task_agent_lite import TaskAgent
from core.config import BotConfig
from core.container import DependencyContainer


FLAT_LEGACY_CONFIG = {
    "model": "glm-5",
    "provider": "zai-coding",
    "tools": ["browser", "task"],
    "max_steps": 25,
    "temperature": 0.0,
    "use_vision": True,
    "tools_config": {},
}


def test_flat_legacy_config_shape_fails_TaskSessionConfig_validation():
    """Documents the contract: the flat session_info['config'] shape (written at
    session creation, see agents/task_agent_lite.py's update_session_metadata call)
    is NOT TaskSessionConfig-compatible and must never be passed as session_config."""
    with pytest.raises(Exception) as exc_info:
        TaskSessionConfig.from_dict(FLAT_LEGACY_CONFIG)
    msg = str(exc_info.value)
    for field in ("model", "provider", "max_steps", "temperature", "use_vision"):
        assert field in msg


def _make_agent(tmp_path):
    config = BotConfig()
    container = DependencyContainer.get_instance(config)
    agent = TaskAgent(config=config, container=container)
    agent.session_manager = SessionManager(base_dir=str(tmp_path))
    agent.task_available = True
    agent._initialized = True
    return agent


@pytest.mark.asyncio
async def test_recreate_orchestrator_does_not_pass_flat_config_as_session_config(
    tmp_path, monkeypatch
):
    """On restore (e.g. a self-wake re-entry) with no explicit request['session_config'],
    _recreate_orchestrator must call create_agent with session_config=None — not the
    flat legacy config — so TaskSessionConfig validation never fires (and never warns)."""
    agent = _make_agent(tmp_path)
    sid = agent.session_manager.create_session("restore-me", user_id="u1")

    session_info = {
        "user_id": "u1",
        "task": "resume task",
        "request": {},  # no explicit session_config
        "config": dict(FLAT_LEGACY_CONFIG),
    }

    mock_orchestrator = MagicMock()
    mock_orchestrator.initialize = AsyncMock()
    mock_orchestrator.create_agent = AsyncMock(return_value=MagicMock(
        message_manager=None, hitl_manager=None, task_context_manager=None,
    ))

    monkeypatch.setattr(
        "agents.task.agent.orchestrator.SessionOrchestrator",
        MagicMock(return_value=mock_orchestrator),
    )
    monkeypatch.setattr(agent, "_get_llm_for_request", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(agent, "_rebind_recreated_chat", MagicMock())
    monkeypatch.setattr(agent, "register_orchestrator", MagicMock())

    result = await agent._recreate_orchestrator(sid, session_info)

    assert result is mock_orchestrator
    mock_orchestrator.create_agent.assert_awaited_once()
    _, kwargs = mock_orchestrator.create_agent.await_args
    assert kwargs["session_config"] is None
