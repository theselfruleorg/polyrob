"""F30 — the emergency prune's contract now matches its code.

The prune opened by scanning ``history.messages`` for a ``SystemMessage`` at
index 0 and for "the first HumanMessage", to preserve them. Neither has been
there for a year: the system prompt and the initial task live in the FOUNDATION
(``_system_message`` / ``_initial_task_message``), stored beside the deque
precisely so it cannot evict them, and ``get_messages_for_llm`` re-prepends
them. So the scan was dead code whose docstring promised a five-message floor
the code never delivered — the two slots it claimed to keep were phantom.

What must hold: the last >= 3 messages survive, no ``AIMessage(tool_calls)`` ->
``ToolMessage`` pair is split, the foundation is untouched and re-prepended, and
the early-out threshold is the same number the keep rule uses.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


@pytest.fixture
def mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="the original task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id="s-prune",
    )


def _turns(mm: MessageManager, n: int) -> None:
    for i in range(n):
        mm._add_message_with_tokens(HumanMessage(content=f"turn {i}"), _internal=True)
        mm._add_message_with_tokens(
            AIMessage(content=f"call {i}",
                      tool_calls=[{"id": f"c{i}", "name": "noop", "args": {}}]),
            _internal=True)
        mm._add_message_with_tokens(
            ToolMessage(content=f"result {i}", tool_call_id=f"c{i}"), _internal=True)


def _kinds(mm: MessageManager) -> list:
    return [type(m.message).__name__ for m in mm.history.messages]


def test_keeps_at_least_the_promised_three_messages(mm: MessageManager):
    _turns(mm, 8)
    mm.emergency_context_prune()
    assert len(mm.history.messages) >= 3


def test_never_leaves_an_orphan_tool_message_at_the_head(mm: MessageManager):
    _turns(mm, 8)
    mm.emergency_context_prune()
    assert _kinds(mm)[0] != "ToolMessage", (
        "the kept tail starts with a ToolMessage whose AIMessage is gone; "
        "repair_tool_message_pairs will drop it on every later request"
    )


def test_the_foundation_is_untouched_and_re_prepended(mm: MessageManager):
    mm.set_skill_message("a pinned skill")
    _turns(mm, 8)
    before = mm._system_message_tokens, mm._initial_task_tokens, mm._skill_message_tokens

    mm.emergency_context_prune()

    assert (mm._system_message_tokens, mm._initial_task_tokens,
            mm._skill_message_tokens) == before
    assembled = mm.get_messages_for_llm(consume_ephemeral=False)
    assert isinstance(assembled[0], SystemMessage)
    assert any("the original task" in str(getattr(m, "content", "")) for m in assembled)


def test_a_minimal_history_is_left_alone(mm: MessageManager):
    mm._add_message_with_tokens(HumanMessage(content="one"), _internal=True)
    mm._add_message_with_tokens(HumanMessage(content="two"), _internal=True)
    mm._add_message_with_tokens(HumanMessage(content="three"), _internal=True)

    mm.emergency_context_prune()

    # 3 <= min_recent_messages: the early-out and the keep rule agree, so
    # nothing is cut and nothing is lost.
    assert len(mm.history.messages) == 3


def test_a_four_message_history_is_pruned_not_skipped(mm: MessageManager):
    """The old `<= 5` early-out refused to prune a history the keep rule would
    have cut to 3 — one more way the promise and the code disagreed."""
    for i in range(4):
        mm._add_message_with_tokens(HumanMessage(content=f"m{i}"), _internal=True)

    mm.emergency_context_prune()

    assert len(mm.history.messages) == 3


def test_tokens_are_recounted_after_the_cut(mm: MessageManager):
    _turns(mm, 8)
    mm.emergency_context_prune()
    assert mm.history.total_tokens == sum(
        m.metadata.input_tokens for m in mm.history.messages)


def test_the_anti_thrash_signal_is_reset(mm: MessageManager):
    _turns(mm, 8)
    mm._compaction_savings = [0.05, 0.04]
    mm.emergency_context_prune()
    assert mm._compaction_savings == []
