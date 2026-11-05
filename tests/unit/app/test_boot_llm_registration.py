"""A clean boot says nothing about the LLM (the prod journal, every start).

``DependencyContainer: Service llm already registered, replacing``: the client factory
took the name ``llm`` for the first client, and the manager then replaced it.
``Preferred LLM client not available``: the agent preferred a fixed provider, which is
missing on every box without that provider's key.
"""
import logging
from types import SimpleNamespace

import pytest

from agents.base_agent import BaseAgent
from core.config import BotConfig
from core.container import DependencyContainer
from modules.llm.llm_manager import LLMManager


@pytest.fixture
def boot(monkeypatch):
    """Only an OpenRouter key: the shape of the box that logged both warnings."""
    from modules.llm.llm_client import LLMClient
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                 "ZAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(LLMClient, "_skip_validate", True)
    monkeypatch.setattr(DependencyContainer, "_instance", None)  # restored after the test
    config = BotConfig()
    return config, DependencyContainer.get_instance(config)


@pytest.mark.asyncio
async def test_the_server_order_registers_llm_once_and_it_is_the_manager(boot, caplog):
    config, container = boot
    from polyrob.app.bootstrap import register_llm_service
    with caplog.at_level(logging.WARNING):
        assert await register_llm_service(container, config, require=True) is True
    assert "already registered" not in caplog.text
    assert isinstance(container.get_service("llm"), LLMManager)


@pytest.mark.asyncio
async def test_a_manager_alone_never_leaves_a_client_under_the_llm_name(boot):
    """What the server's initialize_modules does before it registers the manager."""
    config, container = boot
    manager = LLMManager(name="llm", config=config, container=container)
    await manager.initialize()
    assert manager.clients, "the fixture must bring up a client"
    assert not container.has_service("llm")
    assert container.get_service("llm_client") in manager.clients.values()


@pytest.mark.asyncio
async def test_the_agent_prefers_the_primary_the_manager_chose(boot):
    config, container = boot
    from polyrob.app.bootstrap import register_llm_service
    await register_llm_service(container, config, require=True)
    manager = container.get_service("llm")
    agent = SimpleNamespace(container=container, config=config, name="task_agent",
                            logger=logging.getLogger("t"))
    assert BaseAgent._get_preferred_llm_client(agent) is manager.clients[manager.primary_client_name]


def _agent(services, config=None):
    return SimpleNamespace(container=SimpleNamespace(get_service=services.get),
                           config=config or {}, name="task_agent", logger=logging.getLogger("t"))


def test_the_config_choice_still_wins():
    chosen = object()
    agent = _agent({"llm": SimpleNamespace(primary_client_name="other"), "x_client": chosen,
                    "other": object()}, {"task_agent": {"llm_client": "x_client"}})
    assert BaseAgent._get_preferred_llm_client(agent) is chosen


def test_no_manager_is_no_preference():
    assert BaseAgent._get_preferred_llm_client(_agent({})) is None
