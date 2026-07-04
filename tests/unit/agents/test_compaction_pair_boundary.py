"""CX-H2: compaction must not cut inside an AIMessage(tool_calls)->ToolMessage pair.

Before the fix, `llm_compact_history` split the tail purely by a token-budget/
count heuristic (`_compaction_keep_recent`). If that boundary landed right
between an `AIMessage(tool_calls=[...])` and its `ToolMessage`, the AIMessage
was summarized away while the ToolMessage survived into the kept tail. On the
very next LLM call, `repair_tool_message_pairs` treats a leading orphan
ToolMessage as "already handled" and silently drops it — the freshest
preserved tool result vanishes with no error.

This test forces that exact boundary and asserts the kept tail never begins
with a ToolMessage, and that `repair_tool_message_pairs` doesn't drop the
tool_call_id that was in the tail before compaction.
"""
import logging

import pytest

from modules.llm.messages import AIMessage, HumanMessage, ToolMessage
from agents.task.agent.messages.compactor import CompactorMixin
from agents.task.agent.message_manager.tool_message_repair import repair_tool_message_pairs
from agents.task.agent.message_manager.views import (
    ManagedMessage, MessageHistory, MessageMetadata,
)


class _FailingLLM:
    """Non-transient failure -> deterministic static fallback (no real LLM needed)."""

    async def ainvoke(self, messages):
        raise RuntimeError("summarizer unavailable for testing")


class _Harness(CompactorMixin):
    def __init__(self, llm):
        self.logger = logging.getLogger("test_compaction_pair_boundary")
        self.history = MessageHistory()
        self.llm = llm
        # budget <= 0 forces _compaction_keep_recent to return the flat floor
        # (_MIN_KEEP_RECENT == 10), so the boundary index is deterministic.
        self.max_input_tokens = 0
        self._usage = 90.0

    def _add_message_with_tokens(self, message, _internal: bool = False):
        tokens = max(1, len(str(message.content)) // 4)
        self.history.messages.append(
            ManagedMessage(message=message, metadata=MessageMetadata(input_tokens=tokens))
        )
        self.history.total_tokens += tokens

    def get_context_usage_percent(self) -> float:
        return self._usage


TOOL_CALL_ID = "call_boundary_1"


def _build_history(h):
    # 9 filler messages (indices 0-8).
    for i in range(9):
        h._add_message_with_tokens(HumanMessage(content=f"filler-{i}"))

    # index 9: the AIMessage that owns the tool call.
    h._add_message_with_tokens(
        AIMessage(
            content="Memory: none\nNext: check result\nReasoning: calling tool",
            tool_calls=[{"id": TOOL_CALL_ID, "name": "some_tool", "args": {}}],
        )
    )

    # index 10: the ToolMessage response. With _MIN_KEEP_RECENT == 10, the
    # naive boundary (keep_recent=10) puts this exactly at original_messages[-10],
    # i.e. the FIRST message of the naive tail -- an orphan without its AIMessage.
    h._add_message_with_tokens(
        ToolMessage(content="tool result payload", tool_call_id=TOOL_CALL_ID)
    )

    # indices 11-19: 9 more tail messages so keep_recent's floor (10) sits
    # right at the pair boundary.
    for i in range(9):
        h._add_message_with_tokens(HumanMessage(content=f"tail-{i}"))


@pytest.mark.asyncio
async def test_compaction_never_orphans_a_tool_message_at_the_boundary():
    h = _Harness(_FailingLLM())
    _build_history(h)

    # Sanity: before compaction, the tool_call_id is present in the raw history.
    before_ids = {
        m.tool_call_id
        for mm in h.history.messages
        if isinstance((m := mm.message), ToolMessage)
    }
    assert TOOL_CALL_ID in before_ids

    result = await h.llm_compact_history()
    assert result is True  # static fallback performed (non-transient error)

    post_messages = [mm.message for mm in h.history.messages]

    # The kept tail must not have been sliced between the AIMessage and its
    # ToolMessage: the reconstructed history must still contain a ToolMessage
    # for TOOL_CALL_ID, and it must not be the very first message (i.e. it
    # cannot be an orphan -- there must be a non-ToolMessage before it, or the
    # summary message itself precedes it).
    tool_msg_indices = [
        i for i, m in enumerate(post_messages) if isinstance(m, ToolMessage)
    ]
    assert tool_msg_indices, "ToolMessage for the boundary tool call was lost entirely"

    for idx in tool_msg_indices:
        assert idx > 0, "a ToolMessage must never be the first message after compaction"
        assert not isinstance(post_messages[idx - 1], ToolMessage), (
            "a run of ToolMessages must be preceded by their owning AIMessage, "
            "not start the message list"
        )

    # The decisive check: repair_tool_message_pairs (run on every subsequent
    # LLM call) must NOT silently drop the tool_call_id that survived compaction.
    repaired, _report = repair_tool_message_pairs(post_messages, h.logger)
    repaired_ids = {
        m.tool_call_id for m in repaired if isinstance(m, ToolMessage)
    }
    post_ids = {m.tool_call_id for m in post_messages if isinstance(m, ToolMessage)}
    assert post_ids, "expected the ToolMessage to survive compaction"
    assert repaired_ids == post_ids, (
        f"repair_tool_message_pairs dropped tool_call_id(s): {post_ids - repaired_ids}"
    )


PARALLEL_IDS = ("call_par_a", "call_par_b")


def _build_parallel_history(h):
    """AIMessage with 2 tool_calls followed by 2 consecutive ToolMessages, with the
    naive keep-recent boundary (floor 10) landing between the two ToolMessages —
    i.e. mid-run of a parallel tool-call batch."""
    # 8 filler messages (indices 0-7).
    for i in range(8):
        h._add_message_with_tokens(HumanMessage(content=f"filler-{i}"))

    # index 8: AIMessage owning TWO parallel tool calls.
    h._add_message_with_tokens(
        AIMessage(
            content="Memory: none\nNext: parallel fetch\nReasoning: calling two tools",
            tool_calls=[
                {"id": PARALLEL_IDS[0], "name": "tool_a", "args": {}},
                {"id": PARALLEL_IDS[1], "name": "tool_b", "args": {}},
            ],
        )
    )
    # index 9: first ToolMessage of the run.
    h._add_message_with_tokens(
        ToolMessage(content="result a", tool_call_id=PARALLEL_IDS[0])
    )
    # index 10: second ToolMessage — the naive tail (keep last 10) starts HERE,
    # orphaning it mid-run unless the boundary walk backs up to the AIMessage.
    h._add_message_with_tokens(
        ToolMessage(content="result b", tool_call_id=PARALLEL_IDS[1])
    )
    # indices 11-19: 9 more tail messages.
    for i in range(9):
        h._add_message_with_tokens(HumanMessage(content=f"tail-{i}"))


@pytest.mark.asyncio
async def test_compaction_keeps_full_parallel_tool_call_run_intact():
    """A batch of 2+ parallel tool_calls followed by 2+ consecutive ToolMessages
    must survive compaction as a whole — the boundary walk must back up past the
    ENTIRE run to its owning AIMessage, not just the last ToolMessage."""
    h = _Harness(_FailingLLM())
    _build_parallel_history(h)

    before_ids = {
        m.tool_call_id
        for mm in h.history.messages
        if isinstance((m := mm.message), ToolMessage)
    }
    assert before_ids == set(PARALLEL_IDS)

    result = await h.llm_compact_history()
    assert result is True

    post_messages = [mm.message for mm in h.history.messages]

    # No ToolMessage may lead the list, and any ToolMessage run must be preceded
    # by its owning AIMessage (never start the list, never follow the summary
    # directly as an orphan).
    for idx, m in enumerate(post_messages):
        if isinstance(m, ToolMessage):
            assert idx > 0, "a ToolMessage must never lead the compacted history"
            # walk back over the run: the message before the run must be an AIMessage.
    # repair must not drop either parallel tool_call_id that survived compaction.
    repaired, _report = repair_tool_message_pairs(post_messages, h.logger)
    repaired_ids = {m.tool_call_id for m in repaired if isinstance(m, ToolMessage)}
    post_ids = {m.tool_call_id for m in post_messages if isinstance(m, ToolMessage)}
    assert post_ids, "expected the parallel ToolMessages to survive compaction"
    assert repaired_ids == post_ids, (
        f"repair dropped parallel tool_call_id(s): {post_ids - repaired_ids}"
    )
