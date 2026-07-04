"""PR13: skills are injected as a pinned user message (origin=SKILL), not embedded
in the system prompt — keeps the system prompt clean and the chat schema honest.
"""
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import MessageOrigin


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
    )


def _text(m):
    return m.content if isinstance(m.content, str) else str(m.content)


# get_messages_for_llm() builds the messages actually sent to the LLM
# (llm_runner.py:1044). origin is in-process metadata that may not survive message
# reconstruction, so we assert on the durable on-wire signal: the envelope content.

def test_skill_injected_into_llm_messages_not_system_prompt():
    mm = _mm()
    mm.set_skill_message("USE_TOOL_X_WHEN_Y")
    msgs = mm.get_messages_for_llm()

    # System prompt must NOT contain the skill content (kept stable/cacheable).
    assert "USE_TOOL_X_WHEN_Y" not in _text(msgs[0])

    # Exactly one enveloped skills block, carrying the content, as a user-role msg.
    skill_msgs = [m for m in msgs if "available-skills" in _text(m)]
    assert len(skill_msgs) == 1
    assert "USE_TOOL_X_WHEN_Y" in _text(skill_msgs[0])
    assert skill_msgs[0].to_dict()["role"] == "user"


def test_make_control_message_skill_origin():
    """The skill message is tagged SKILL at creation (origin verified at the source)."""
    from modules.llm.messages import make_control_message
    m = make_control_message("skills here", MessageOrigin.SKILL)
    assert m.origin == MessageOrigin.SKILL
    assert "<available-skills>" in m.content


def test_no_skill_block_when_unset():
    mm = _mm()
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "available-skills" in _text(m)]


def test_set_empty_skill_message_is_noop():
    mm = _mm()
    mm.set_skill_message("")
    msgs = mm.get_messages_for_llm()
    assert not [m for m in msgs if "available-skills" in _text(m)]
