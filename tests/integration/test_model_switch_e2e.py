"""094 end to end: `/model` on a REAL TaskAgent + orchestrator + Agent.

Only the network is stubbed (the LLM build, the LLM decision and the OpenRouter
model list). The test proves: the verb queues the pin on the live agent and
writes the session record; the next turn's drain applies it before the first LLM
call; the swapped agent runs on the new model with the reasoning level scoped to
its main call; the identity line names the previous model; a new owner chat
created with a pin carries it in its record.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.task.agent.conversation import Conversation
from agents.task.agent.views import AgentBrain, AgentOutput

CATALOG = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash", "context_length": 1048576,
     "pricing": {"prompt": "0.000000045", "completion": "0.0000005"},
     "supported_parameters": ["tools", "reasoning"]},
    {"id": "anthropic/claude-haiku-5.5", "context_length": 1000000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
     "supported_parameters": ["tools", "reasoning"]},
]}


class _Chat:
    def __init__(self, model):
        self.model_name = model
        self._default_temperature = 0.0

    async def ainvoke(self, *a, **k):  # the decision is stubbed
        raise AssertionError("no LLM call in this test")


def _env(monkeypatch, tmp_path):
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "polyrob_data"))
    monkeypatch.setenv("MEMORY_BACKEND", "none")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("DEFAULT_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    import modules.llm.openrouter_catalog as oc
    monkeypatch.setattr(oc, "_fetch", lambda: CATALOG)
    monkeypatch.setattr(oc, "_cache_path", lambda: tmp_path / "or_models.json")
    monkeypatch.setattr(oc, "_MEM", {"at": 0.0, "rows": None})
    import modules.llm.profiles as profiles
    monkeypatch.setattr(profiles, "providers_with_keys", lambda env=None: ["openrouter"])


@pytest.mark.asyncio
async def test_model_switch_end_to_end(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from polyrob.app.bootstrap import build_cli_container
        container = await build_cli_container(log_level="ERROR")
    task_agent = container.get_agent("task_agent")

    builds = []

    async def fake_get_chat_model(self, provider, model, **kw):
        builds.append((provider, model, kw.get("isolated_client")))
        return _Chat(model)

    monkeypatch.setattr("modules.llm.llm_manager.LLMManager.get_chat_model",
                        fake_get_chat_model)

    info = await task_agent.create_session(user_id="local", request={
        "task": "chat", "model": "deepseek/deepseek-v4.1-flash", "provider": "openrouter",
        "tools": ["filesystem", "task"], "max_steps": 2, "use_vision": False,
    }, skip_credit_check=True)
    sid = info["id"]
    orch = task_agent.get_orchestrator(sid)
    agent = await orch.create_agent(task="chat", llm=_Chat("deepseek/deepseek-v4.1-flash"),
                                    agent_name="executor", use_vision=False,
                                    max_actions_per_step=5)
    agent.stall_timeout_seconds = None
    agent.generate_gif = False
    agent.validate_output = False
    agent.llm_provider = "openrouter"
    prompt_before = agent.message_manager._system_message.content

    # --- the verb, as Telegram runs it ------------------------------------------
    from agents.task.model_switch_service import model_command
    out = await model_command(task_agent, user_id="local", words=["haiku", "reasoning=low"],
                              session_id=sid, chat_key="telegram:dm:1")
    assert out.startswith("✅"), out
    assert agent.pending_model_pin()["model"] == "anthropic/claude-haiku-5.5"
    record = task_agent.session_manager.get_session_info(sid)
    assert record["request"]["model"] == "anthropic/claude-haiku-5.5"
    assert record["operator_pin"] is None and record["model_pin"]["reasoning"] == "low"
    assert agent.model_name == "deepseek/deepseek-v4.1-flash"  # not yet: next step

    # --- the next turn: the drain applies it before the first LLM call ----------
    from modules.llm.openrouter_reasoning import reasoning_extra_body
    action_model = agent.controller.registry.create_action_model()
    seen = {}

    async def decide(input_messages):
        seen["model"] = agent.model_name
        seen["reasoning"] = reasoning_extra_body(4000)
        return AgentOutput(current_state=AgentBrain(evaluation_previous_goal="-", memory="-",
                                                    next_goal="-"),
                           action=[action_model(done={"text": "hi"})])

    agent._get_next_action_unscoped = decide
    from agents.task.agent.core import model_swap  # the swap path builds via LLMManager
    assert model_swap
    answer = await Conversation(agent).respond("hello", max_steps=2)
    assert answer
    assert seen["model"] == "anthropic/claude-haiku-5.5"
    assert seen["reasoning"] == {"reasoning": {"effort": "low"}}
    assert ("openrouter", "anthropic/claude-haiku-5.5", True) in builds   # isolated client
    assert agent.pending_model_pin() is None
    ident = agent.message_manager._runtime_identity_message.content
    assert "anthropic/claude-haiku-5.5" in ident and "deepseek/deepseek-v4.1-flash" in ident
    assert agent.message_manager._system_message.content is not None
    assert prompt_before  # rebuilt prompt exists (content may match for this family)
    # outside the main call the env level holds
    assert reasoning_extra_body(4000) is None

    # --- /model shows the live state -------------------------------------------
    shown = await model_command(task_agent, user_id="local", words=[], session_id=sid)
    assert "Model: anthropic/claude-haiku-5.5 (openrouter)" in shown
    assert "Reasoning: low" in shown

    # --- reset follows the operator pin again ------------------------------------
    out = await model_command(task_agent, user_id="local", words=["reset"], session_id=sid)
    assert "Back to deepseek/deepseek-v4.1-flash" in out
    assert task_agent.session_manager.get_session_info(sid)["operator_pin"] == {
        "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}


@pytest.mark.asyncio
async def test_new_owner_chat_starts_on_the_pin(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    with patch("modules.llm.llm_manager.LLMManager._initialize", AsyncMock()):
        from polyrob.app.bootstrap import build_cli_container
        container = await build_cli_container(log_level="ERROR")
    task_agent = container.get_agent("task_agent")
    import core.runtime_paths as rp
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: str(tmp_path / "prefs"))

    from agents.task.model_switch_service import model_command, owner_chat_pin, start_with_pin
    from agents.task.model_pin_state import session_reasoning
    from agents.task.task_agent_support import SessionRequest
    out = await model_command(task_agent, user_id="local", words=["haiku", "default",
                                                                  "reasoning=off"],
                              chat_key="telegram:dm:7")
    assert "your default" in out, out
    pin = owner_chat_pin("telegram:dm:7", "local")
    assert pin["model"] == "anthropic/claude-haiku-5.5"
    info = await task_agent.create_session(
        user_id="local", request=SessionRequest(task="hi", provider=pin["provider"],
                                                model=pin["model"],
                                                tools=["filesystem", "task"]),
        skip_credit_check=True)
    start_with_pin(task_agent, info["id"], pin)
    record = task_agent.session_manager.get_session_info(info["id"])
    assert record["request"]["model"] == "anthropic/claude-haiku-5.5"
    assert record["operator_pin"] is None
    assert session_reasoning(info["id"]) == "off"
