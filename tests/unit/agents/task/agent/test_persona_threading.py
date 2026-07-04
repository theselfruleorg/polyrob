"""S1 — persona_block threads AgentConfig -> from_params -> MessageManager -> SystemPrompt."""
from unittest.mock import MagicMock

from agents.task.agent.service import Agent, AgentConfig


def test_agent_config_has_persona_block_default_none():
    c = AgentConfig(task="t")
    assert c.persona_block is None


def test_from_params_routes_persona_block(monkeypatch):
    captured = {}
    monkeypatch.setattr(Agent, "__init__", lambda self, config, deps: captured.update(config=config))
    Agent.from_params(task="t", llm=MagicMock(), orchestrator=MagicMock(), persona_block="You are Rob.")
    assert captured["config"].persona_block == "You are Rob."


def test_message_manager_forwards_persona_block_to_system_prompt():
    # The no-profile branch builds SystemPrompt internally; assert persona_block
    # reaches the prompt class constructor (capture via a fake system_prompt_class).
    from agents.task.agent.message_manager.service import MessageManager

    captured = {}

    class FakePrompt:
        def __init__(self, action_descriptions, **kw):
            captured.update(kw)

        def get_system_message(self):
            from modules.llm.messages import SystemMessage
            return SystemMessage(content="sys")

    llm = MagicMock()
    llm.model_type = "gpt-4"
    MessageManager(
        llm=llm,
        task="t",
        action_descriptions="x",
        system_prompt_class=FakePrompt,
        persona_block="You are Rob.",
    )
    assert captured.get("persona_block") == "You are Rob."
