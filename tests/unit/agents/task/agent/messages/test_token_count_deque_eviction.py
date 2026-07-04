"""Regression (MM-2): total_tokens must not drift when the bounded history deque
auto-evicts on append.

deque(maxlen).append() drops the leftmost message without decrementing
total_tokens, and the trim loop can't compensate (len never exceeds maxlen), so
total_tokens crept upward vs the actual deque contents — tripping compaction
thresholds early. After the fix, total_tokens equals the sum of the current
contents' tokens.
"""
from collections import deque
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import HumanMessage


class _TCM:
    def get_context_injection(self, session_id):
        return None


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
        task_context_manager=_TCM(), session_id="s1",
    )


def _sum_tokens(mm):
    return sum(m.metadata.input_tokens for m in mm.history.messages
              if getattr(m, "metadata", None))


def test_total_tokens_matches_contents_after_eviction():
    mm = _mm()
    # Force a small bounded history so appends trigger maxlen eviction quickly.
    cur = list(mm.history.messages)
    mm.history.max_messages = 4
    mm.history.messages = deque(cur, maxlen=4)
    mm.history.total_tokens = _sum_tokens(mm)

    for i in range(12):
        mm._add_message_with_tokens(
            HumanMessage(content=f"message number {i} with a few tokens of content"),
            _internal=True,
        )

    # The bug let total_tokens grow past the true content total; after the fix they match.
    assert mm.history.total_tokens == _sum_tokens(mm)
    assert len(mm.history.messages) == 4
