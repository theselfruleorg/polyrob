"""MessageManager starts a session with exactly a system message + the task message.

Salvaged from the in-package `agents/task/agent/message_manager/tests.py`, which pytest
never collected (a file named `tests.py` does not match the default `python_files`
pattern). The four sibling tests in that file asserted a browser-state rendering format
that has since moved on; the state-message contract is covered by
`tests/unit/agents/test_state_message_removal.py` and
`tests/unit/agents/task/agent/test_message_builders_split.py`, so only this
construction smoke test carried unique value and moves here.
"""

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import HumanMessage, SystemMessage


class _FakeChatModel:
    """MessageManager only reads ``model_name`` off the LLM — no network, no keys."""

    def __init__(self, model_name: str):
        self.model_name = model_name


@pytest.fixture(
    params=[_FakeChatModel('gpt-5'), _FakeChatModel('claude-sonnet-4-5')],
    ids=['gpt-5', 'claude-sonnet-4-5'],
)
def message_manager(request: pytest.FixtureRequest) -> MessageManager:
    return MessageManager(
        llm=request.param,
        task='Test task',
        action_descriptions='Test actions',
        system_prompt_class=SystemPrompt,
        max_input_tokens=1000,
        image_tokens=800,
    )


def test_initial_messages(message_manager: MessageManager):
    messages = message_manager.get_messages()
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert 'Test task' in messages[1].content
