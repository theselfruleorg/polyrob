"""B-4 (2026-10-14 review): a live owner chat follows a moved operator pin on
recreate; an explicit per-session model is kept."""
import pytest

from agents.task.task_agent_support import operator_pin_stamp, refresh_pinned_model

NEW = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "z-ai/glm-5.3-flash"}
OLD = {"DEFAULT_PROVIDER": "openrouter", "DEFAULT_MODEL": "deepseek/deepseek-v4.1-flash"}


def _info(model, provider="openrouter", **extra):
    pair = {"provider": provider, "model": model}
    return {"request": dict(pair), "config": dict(pair), **extra}


def test_stamp_only_when_created_on_the_pin():
    assert operator_pin_stamp("openrouter", "deepseek/deepseek-v4.1-flash", env=OLD) == {
        "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}
    assert operator_pin_stamp("openrouter", "x-ai/grok-4.3", env=OLD) is None


def test_a_session_on_the_old_pin_follows_the_new_pin():
    stamp = operator_pin_stamp("openrouter", "deepseek/deepseek-v4.1-flash", env=OLD)
    info = _info("deepseek/deepseek-v4.1-flash", operator_pin=stamp)
    assert refresh_pinned_model(info, env=NEW) == ("openrouter", "z-ai/glm-5.3-flash")
    # unchanged pin -> keep
    assert refresh_pinned_model(info, env=OLD) is None


def test_an_explicit_model_is_kept():
    info = _info("x-ai/grok-4.3", operator_pin=None)       # stamped None at creation
    assert refresh_pinned_model(info, env=NEW) is None
    # a stamp that does not match the stored pair (a later explicit choice) is kept too
    info = _info("x-ai/grok-4.3", operator_pin={"provider": "openrouter",
                                                 "model": "deepseek/deepseek-v4.1-flash"})
    assert refresh_pinned_model(info, env=NEW) is None


@pytest.mark.parametrize("extra,expected", [
    ({"chat_session_key": "agent:main:telegram:dm:1"}, ("openrouter", "z-ai/glm-5.3-flash")),
    ({"creator": "owner"}, ("openrouter", "z-ai/glm-5.3-flash")),
    ({"creator": "cron"}, None),                       # not an owner chat
    ({}, None),
])
def test_legacy_records_follow_only_for_owner_chat(extra, expected):
    assert refresh_pinned_model(_info("deepseek/deepseek-v4.1-flash", **extra), env=NEW) == expected


def test_legacy_owner_chat_on_another_provider_is_kept():
    info = _info("claude-sonnet-4-5", provider="anthropic", creator="owner")
    assert refresh_pinned_model(info, env=NEW) is None


def test_missing_model_is_left_to_the_resolver():
    assert refresh_pinned_model({"request": {}, "config": {}}, env=NEW) is None


@pytest.mark.asyncio
async def test_recreate_builds_the_llm_on_the_new_pin_and_persists_it(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from agents.task.agent.session import SessionManager
    from agents.task_agent_lite import TaskAgent
    from core.config import BotConfig
    from core.container import DependencyContainer

    for k, v in NEW.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("CHAT_MODEL", raising=False)
    config = BotConfig()
    agent = TaskAgent(config=config, container=DependencyContainer.get_instance(config))
    agent.session_manager = SessionManager(base_dir=str(tmp_path))
    agent.task_available, agent._initialized = True, True
    sid = agent.session_manager.create_session("owner-chat", user_id="u1")
    info = {"user_id": "u1", "task": "hi", "creator": "owner",
            **_info("deepseek/deepseek-v4.1-flash")}

    orch = MagicMock()
    orch.initialize = AsyncMock()
    orch.create_agent = AsyncMock(return_value=MagicMock(
        message_manager=None, hitl_manager=None, task_context_manager=None))
    monkeypatch.setattr("agents.task.agent.orchestrator.SessionOrchestrator",
                        MagicMock(return_value=orch))
    get_llm = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(agent, "_get_llm_for_request", get_llm)
    monkeypatch.setattr(agent, "_rebind_recreated_chat", MagicMock())
    monkeypatch.setattr(agent, "register_orchestrator", MagicMock())

    await agent._recreate_orchestrator(sid, info)
    req = get_llm.await_args.args[0]
    assert (req["provider"], req["model"]) == ("openrouter", "z-ai/glm-5.3-flash")
    stored = agent.session_manager.get_session_info(sid)
    assert stored["request"]["model"] == "z-ai/glm-5.3-flash"
    assert stored["operator_pin"] == {"provider": "openrouter", "model": "z-ai/glm-5.3-flash"}
