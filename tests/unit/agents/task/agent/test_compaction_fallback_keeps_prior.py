"""BUG 4 fix: static-fallback path must NOT drop the prior compacted summary.

Before the fix, the `except Exception` branch in llm_compact_history computed
`static = self._build_static_fallback_summary(to_summarize)` and passed it
directly to `_rebuild_with_summary`, discarding `prior_summary`.  On iterative
compaction (more than one compaction cycle) this silently erased the earlier
accumulated digest, causing data loss.

This test simulates the scenario:
  1.  A prior compacted summary is already present in the message history
      (tagged MessageOrigin.COMPACTION_SUMMARY / containing _COMPACTED_MARKER).
  2.  The LLM summarizer raises a non-transient error.
  3.  The static fallback is applied.
  4.  The resulting compacted message must STILL contain the prior summary's
      distinctive text — it must not have been dropped.
"""
import logging

import pytest

from modules.llm.messages import AIMessage, HumanMessage, MessageOrigin, make_control_message
from agents.task.agent.messages.compactor import CompactorMixin, _COMPACTED_MARKER
from agents.task.agent.message_manager.views import (
    ManagedMessage, MessageHistory, MessageMetadata,
)


class _FailingLLM:
    """Always raises a non-transient RuntimeError."""
    async def ainvoke(self, messages):
        raise RuntimeError("summarizer unavailable for testing")


class _Harness(CompactorMixin):
    def __init__(self, llm):
        self.logger = logging.getLogger("test_compaction_fallback_keeps_prior")
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
        # Should not be called — static fallback should have content.
        raise AssertionError("emergency_context_prune called unexpectedly")


PRIOR_SUMMARY_SENTINEL = "PRIOR_SUMMARY_DISTINCTIVE_SENTINEL_XYZ"


def _fill(h, n=20, size=500):
    """Add enough regular messages to exceed the minimum compaction threshold."""
    for i in range(n):
        h._add_message_with_tokens(HumanMessage(content=("x" * size) + f"-msg{i}"))


@pytest.mark.asyncio
async def test_static_fallback_preserves_prior_summary():
    """The prior compacted summary text must appear in the fallback output."""
    h = _Harness(_FailingLLM())

    # Inject a "prior compacted summary" message first (using the marker so the
    # compactor's A4 detection picks it up regardless of origin attribute).
    prior_body = f"{_COMPACTED_MARKER}\n\n{PRIOR_SUMMARY_SENTINEL}\n\nSome earlier facts."
    h._add_message_with_tokens(HumanMessage(content=prior_body))

    # Add enough regular messages to trigger compaction.
    _fill(h)

    result = await h.llm_compact_history()
    assert result is True, "static fallback should report success"

    # Inspect the compacted history message that replaced the middle.
    compacted_texts = [
        str(mm.message.content)
        for mm in h.history.messages
    ]
    combined = "\n".join(compacted_texts)
    assert PRIOR_SUMMARY_SENTINEL in combined, (
        f"prior compacted summary was dropped from the fallback output. "
        f"Compacted messages:\n{combined[:800]}"
    )
