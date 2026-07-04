"""B16 (medium) — an EMPTY LLM compaction summary must not wipe the history.

_run_summarization returns "" when the compaction LLM yields empty content (no
exception). _rebuild_with_summary would then clear all messages and insert an
empty digest → permanent data loss. Fix: on an empty summary, fall back to the
static summary; if that is empty too, abort and keep the FULL context.
"""
import logging

import pytest

from modules.llm.messages import AIMessage, HumanMessage
from agents.task.agent.messages.compactor import CompactorMixin, _COMPACTED_MARKER
from agents.task.agent.message_manager.views import (
    ManagedMessage, MessageHistory, MessageMetadata,
)


class _EmptyLLM:
    """Returns an empty-content response (no exception)."""
    async def ainvoke(self, messages):
        return AIMessage(content="")


class _Harness(CompactorMixin):
    def __init__(self, llm):
        self.logger = logging.getLogger("test_compaction_empty")
        self.history = MessageHistory()
        self.llm = llm
        self.max_input_tokens = 1000
        self._usage = 90.0

    def _add_message_with_tokens(self, message, _internal: bool = False):
        tokens = max(1, len(str(message.content)) // 4)
        self.history.messages.append(
            ManagedMessage(message=message, metadata=MessageMetadata(input_tokens=tokens))
        )
        self.history.total_tokens += tokens

    def get_context_usage_percent(self) -> float:
        return self._usage

    def emergency_context_prune(self):
        raise AssertionError("emergency_context_prune called unexpectedly")


def _fill(h, n=25, size=500):
    for i in range(n):
        h._add_message_with_tokens(HumanMessage(content=("x" * size) + f"-msg{i}"))


@pytest.mark.asyncio
async def test_empty_llm_summary_does_not_wipe_history():
    h = _Harness(_EmptyLLM())
    _fill(h)
    before = len(h.history.messages)

    result = await h.llm_compact_history()

    if result is True:
        # A digest was written (static fallback had content) — it must NOT be empty.
        digest = next(m for m in h.history.messages
                      if _COMPACTED_MARKER in str(m.message.content))
        body = str(digest.message.content)
        summary_section = body.split("earlier messages:", 1)[-1].split("[END")[0].strip()
        assert summary_section, "empty digest replaced the history (data loss)"
    else:
        # Aborted → full context preserved, nothing lost.
        assert len(h.history.messages) == before
