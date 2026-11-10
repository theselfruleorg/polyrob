"""Proposal 106 cause 6, the session side: aux models move on a fallback and on a restore,
the serving provider is excluded, replaced clients close, and the session returns after
the cooldown (R5, R6, R7, L12)."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.agent.core import model_swap
from agents.task.agent.core.model_swap import FALLBACK_COOLDOWN_SEC, close_agent_llms
from agents.task.agent.service import Agent


def _llm(model, provider, closed=None):
    client = SimpleNamespace(name=f"{provider}_client", _spec=SimpleNamespace(name=provider))
    client.cleanup = lambda: closed.append(model) if closed is not None else None
    return SimpleNamespace(model_name=model, _client=client)


def _agent(closed, *, manager=None):
    agent = object.__new__(Agent)
    agent.logger = logging.getLogger("t.failover")
    agent.llm = _llm("deepseek-flash", "deepseek", closed)
    agent.llm_provider = "deepseek"
    agent.adopted = []

    def _adopt(llm, model, provider):
        agent.llm, agent.llm_provider = llm, provider
        agent.__dict__["model_name_value"] = model
        agent.adopted.append(model)
    agent.adopt_active_llm = _adopt
    agent._refresh_after_model_change = MagicMock()
    agent.state = MagicMock()
    agent.state.llm_providers_failed = []
    aux = iter([_llm(f"aux{i}", "x", closed) for i in range(9)])
    agent.message_manager = SimpleNamespace(aux_llm=next(aux))
    agent._provision_compaction_llm = lambda: next(aux)
    agent._judge_llm = None
    agent.task_context_manager = None
    agent.orchestrator = SimpleNamespace(
        session_id="s", user_id=None,
        container=SimpleNamespace(get_service=lambda name: manager))
    return agent


@pytest.fixture(autouse=True)
def _model_name(monkeypatch):
    """The real property delegates to the MessageManager; here the test holds the value."""
    monkeypatch.setattr(Agent, "model_name", property(
        lambda self: self.__dict__.get("model_name_value", "deepseek-flash")))


def test_aux_moves_on_fallback_and_returns_on_restore():
    closed = []
    agent = _agent(closed)
    aux_first = agent.message_manager.aux_llm
    fallback = _llm("deepseek/deepseek-v4.1-flash", "openrouter", closed)
    provider = asyncio.run(agent.adopt_fallback_llm(fallback, "LLMRateLimitError"))

    assert provider == "openrouter"
    assert agent._swap_built_llm is fallback, "the session owns the fallback client"
    aux_on_fallback = agent.message_manager.aux_llm
    assert aux_on_fallback is not aux_first
    assert closed == ["aux0"], "the replaced aux client closes"
    agent._refresh_after_model_change.assert_called_with(
        "fallback", "deepseek-flash", reason="LLMRateLimitError")

    assert asyncio.run(agent.restore_first_llm())
    assert agent.llm.model_name == "deepseek-flash"
    assert agent.llm_provider == "deepseek"
    assert agent.message_manager.aux_llm is not aux_on_fallback
    assert agent._swap_built_llm is None
    assert closed == ["aux0", "deepseek/deepseek-v4.1-flash", "aux1"]
    assert agent._refresh_after_model_change.call_args.args[0] == "restore"


def test_session_returns_only_after_the_cooldown(monkeypatch):
    manager = MagicMock()
    manager.provider_ready = MagicMock(return_value=True)
    agent = _agent([], manager=manager)
    now = [1000.0]
    monkeypatch.setattr(model_swap.time, "monotonic", lambda: now[0])
    asyncio.run(agent.adopt_fallback_llm(_llm("fb", "openrouter"), "LLMRateLimitError"))

    now[0] += FALLBACK_COOLDOWN_SEC - 1
    asyncio.run(agent.return_to_first_llm_if_due())
    assert agent.llm.model_name == "fb"

    now[0] += 2
    manager.provider_ready.return_value = False
    asyncio.run(agent.return_to_first_llm_if_due())
    assert agent.llm.model_name == "fb", "the first provider is not ready yet"

    manager.provider_ready.return_value = True
    asyncio.run(agent.return_to_first_llm_if_due())
    assert agent.llm.model_name == "deepseek-flash"
    manager.provider_ready.assert_called_with("deepseek")
    agent.state.reset_llm_errors.assert_called_once_with(reset_circuit_breaker=True)
    assert agent._first_model is None


def test_a_second_fallback_keeps_the_first_model_and_closes_the_first_fallback():
    closed = []
    agent = _agent(closed)
    asyncio.run(agent.adopt_fallback_llm(_llm("fb1", "openrouter", closed), "A"))
    asyncio.run(agent.adopt_fallback_llm(_llm("fb2", "zai", closed), "B"))
    assert "fb1" in closed
    assert agent._first_model.model == "deepseek-flash"
    asyncio.run(agent.restore_first_llm())
    assert agent.llm.model_name == "deepseek-flash"
    assert "fb2" in closed


def test_a_first_model_from_an_owner_swap_stays_owned():
    closed = []
    agent = _agent(closed)
    own = agent.llm
    agent._swap_built_llm = own
    asyncio.run(agent.adopt_fallback_llm(_llm("fb", "openrouter", closed), "A"))
    assert "deepseek-flash" not in closed, "it serves again after the cooldown"
    asyncio.run(agent.restore_first_llm())
    assert agent._swap_built_llm is own

    asyncio.run(agent.adopt_fallback_llm(_llm("fb", "openrouter", closed), "A"))
    asyncio.run(close_agent_llms(agent))
    assert "deepseek-flash" in closed, "the session end closes the first model it owns"


def test_an_owner_switch_ends_the_return():
    closed = []
    agent = _agent(closed)
    own = agent.llm
    agent._swap_built_llm = own
    asyncio.run(agent.adopt_fallback_llm(_llm("fb", "openrouter", closed), "A"))
    agent._create_llm_from_config_async = AsyncMock(return_value=_llm("glm", "zai", closed))
    assert asyncio.run(agent.swap_model("zai", "glm"))["ok"]
    assert agent._first_model is None
    assert "fb" in closed and "deepseek-flash" in closed


def test_exclusion_uses_the_serving_provider():
    """R6: glm-5.3 on zai-coding failed, so OpenRouter stays eligible."""
    agent = object.__new__(Agent)
    agent.logger = logging.getLogger("t.failover")
    agent.__dict__["model_name_value"] = "glm-5.3"
    agent.llm = _llm("glm-5.3", "zai-coding")
    agent.orchestrator = SimpleNamespace(session_id="s", user_id=None, container=None)
    agent.state = SimpleNamespace(llm_providers_failed=[])
    agent._get_fallback_llm = AsyncMock(return_value=None)
    asyncio.run(agent._attempt_llm_fallback_in_handler("LLMConnectionError"))
    assert agent._get_fallback_llm.await_args.kwargs["exclude_providers"] == ["zai-coding"]


def test_money_run_signal(monkeypatch):
    agent = object.__new__(Agent)
    agent.logger = logging.getLogger("t.failover")
    agent.__dict__["model_name_value"] = "deepseek/deepseek-v4-pro"
    agent.orchestrator = SimpleNamespace(session_id="s", user_id=None, container=None)
    agent.controller = MagicMock()
    agent.controller.get_action_names = MagicMock(return_value=["wallet_send"])
    monkeypatch.delenv("HEAVY_MODEL", raising=False)
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: False)
    assert not agent._is_money_run(), "an owner chat keeps its model choice"
    monkeypatch.setenv("HEAVY_MODEL", "deepseek/deepseek-v4-pro")
    assert agent._is_money_run()
    monkeypatch.delenv("HEAVY_MODEL")
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    monkeypatch.setattr("core.money.classify.money_action", lambda name: name == "wallet_send")
    assert agent._is_money_run()
    agent.controller.get_action_names.return_value = ["web_fetch"]
    assert not agent._is_money_run()


def test_money_run_follows_the_runners_money_class_stamp(monkeypatch):
    """112 F2: a money-class run (``priority=money`` on a non-money rig) that is NOT on
    HEAVY_MODEL is still a money run for the fallback ladder — one predicate."""
    agent = object.__new__(Agent)
    agent.logger = logging.getLogger("t.failover")
    agent.__dict__["model_name_value"] = "deepseek/deepseek-v4.1-flash"
    agent.orchestrator = SimpleNamespace(session_id="s", user_id=None, container=None,
                                         _money_class=True)
    agent.controller = MagicMock()
    agent.controller.get_action_names = MagicMock(return_value=["web_fetch"])
    monkeypatch.delenv("HEAVY_MODEL", raising=False)
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    assert agent._is_money_run()
    agent.orchestrator._money_class = False
    assert not agent._is_money_run()


def test_a_client_a_running_child_holds_is_not_closed_until_the_session_ends():
    """A child inherits the parent's model by reference: the return to the first model
    must not close the fallback under a running delegation."""
    closed = []
    agent = _agent(closed)
    fallback = _llm("deepseek/deepseek-v4.1-flash", "openrouter", closed)
    asyncio.run(agent.adopt_fallback_llm(fallback, "LLMRateLimitError"))
    child = SimpleNamespace(llm=fallback, _is_sub_agent=True)
    agent.orchestrator.agents = {"main": agent, "child": child}

    assert asyncio.run(agent.restore_first_llm())
    assert "deepseek/deepseek-v4.1-flash" not in closed
    assert fallback in agent._retired_llms

    asyncio.run(model_swap.close_session_llms(agent))
    assert "deepseek/deepseek-v4.1-flash" in closed
