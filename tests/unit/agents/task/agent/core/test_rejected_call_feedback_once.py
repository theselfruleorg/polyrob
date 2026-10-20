"""Release review: the rejected-tool-call note answers ONE call. A step that
produced no action re-queues its consumed ephemerals (C-1), so an untagged note
rode again beside the next rejection's note — N failed steps, N stacked notes."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core.next_action_internal import NextActionInternalMixin
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="t", action_descriptions="a",
                          system_prompt_class=SystemPrompt, max_input_tokens=8000,
                          session_id="s-reject")


def _notes(msgs) -> int:
    return sum("rejected before execution" in str(getattr(m, "content", "")) for m in msgs)


def test_repeated_rejections_show_one_note_per_call():
    mm = _mm()
    host = SimpleNamespace(message_manager=mm, logger=logging.getLogger("t"))
    push = NextActionInternalMixin._push_rejected_tool_calls_feedback
    push(host, "'x' is not a registered action")
    for _ in range(3):
        sent = mm.get_messages_for_llm()
        assert _notes(sent) == 1
        # the step rejected the call again: no action, so C-1 restores the rail
        push(host, "'x' is not a registered action")
        mm.restore_ephemeral_on_failure()
    assert _notes(mm.get_messages_for_llm()) == 1
