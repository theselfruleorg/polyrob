"""Regression (P1 finalization): MessageManager.from_config (the advertised DI entry
point) crashed with TypeError — it passed max_input_tokens/max_actions_per_step both
explicitly and via **config.to_dict() ("got multiple values"), and to_dict() also
carried keys __init__ no longer accepts (message_context). It must construct cleanly.
"""
from unittest.mock import MagicMock

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.message_manager.config import MessageManagerConfig
from agents.task.agent.prompts import SystemPrompt


def test_from_config_constructs_without_typeerror():
    cfg = MessageManagerConfig()
    mm = MessageManager.from_config(
        llm=MagicMock(), task="t", action_descriptions="a",
        system_prompt_class=SystemPrompt, config=cfg,
    )
    assert mm.max_input_tokens == cfg.max_input_tokens
    assert mm.max_actions_per_step == cfg.max_actions_per_step


def test_from_config_still_requires_config():
    import pytest
    with pytest.raises(ValueError):
        MessageManager.from_config(
            llm=MagicMock(), task="t", action_descriptions="a",
            system_prompt_class=SystemPrompt, config=None,
        )
