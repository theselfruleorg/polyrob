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
    # F10: eviction is BATCHED, so the deque sits at or under its bound, not on it.
    assert len(mm.history.messages) <= 4
    assert mm.history.messages, "a trimmed history is not an empty one"


def test_eviction_is_batched_and_logged_once(caplog):
    """N appends after saturation must produce ONE eviction event, not N."""
    import logging

    mm = _mm()
    maxlen = 40
    mm.history.max_messages = maxlen
    mm.history.messages = deque(list(mm.history.messages), maxlen=maxlen)
    mm.history.total_tokens = _sum_tokens(mm)

    for i in range(maxlen):
        mm._add_message_with_tokens(HumanMessage(content=f"filler {i}"), _internal=True)
    assert len(mm.history.messages) == maxlen  # saturated, nothing evicted yet

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # batch = max(8, 40 // 10) = 8, so 8 further appends fit in the freed room.
        for i in range(8):
            mm._add_message_with_tokens(HumanMessage(content=f"after {i}"), _internal=True)

    evictions = [r for r in caplog.records if "History saturated" in r.getMessage()]
    assert len(evictions) == 1, f"expected exactly one eviction, saw {len(evictions)}"
    assert mm.history.total_tokens == _sum_tokens(mm)


def test_eviction_never_leaves_an_orphan_tool_message_at_the_head():
    from modules.llm.messages import AIMessage, ToolMessage

    mm = _mm()
    maxlen = 20
    mm.history.max_messages = maxlen
    mm.history.messages = deque(list(mm.history.messages), maxlen=maxlen)
    mm.history.total_tokens = _sum_tokens(mm)

    # An AI(tool_calls) -> Tool pair every two messages; a naive batch of 8 with
    # an odd offset would land the cut on a ToolMessage.
    mm._add_message_with_tokens(HumanMessage(content="kick off"), _internal=True)
    for i in range(maxlen):
        mm._add_message_with_tokens(
            AIMessage(content=f"call {i}",
                      tool_calls=[{"id": f"c{i}", "name": "noop", "args": {}}]),
            _internal=True)
        mm._add_message_with_tokens(
            ToolMessage(content=f"result {i}", tool_call_id=f"c{i}"), _internal=True)

    head = mm.history.messages[0].message
    assert not isinstance(head, ToolMessage), (
        "the batch cut left an orphan ToolMessage at the head of the history"
    )
