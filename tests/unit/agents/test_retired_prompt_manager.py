"""Review F16 (2026-09-29): SystemPromptManager was unused but rewrote
data/prompts/system_prompts.json on every shutdown, and the tree shipped that file
(with a jailbreak-style "simulator_agent" prompt) plus an unused
autov2_prompts.json through the `graft data/prompts` / package-data globs."""
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("name", ["system_prompts.json", "autov2_prompts.json"])
def test_the_dead_prompt_files_are_not_shipped(name):
    assert not (_REPO / "data" / "prompts" / name).exists()


def test_the_prompt_manager_is_retired():
    import agents
    import agents.prompt as ap
    assert not hasattr(ap, "SystemPromptManager")
    with pytest.raises(AttributeError):
        agents.SystemPromptManager  # noqa: B018
    assert ap.DEFAULT_PROMPTS_DIR == _REPO / "data" / "prompts"


def test_message_manager_add_new_task_is_gone():
    from agents.task.agent.message_manager.service import MessageManager
    assert not hasattr(MessageManager, "add_new_task")
