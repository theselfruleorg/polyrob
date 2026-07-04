"""Phase 0.3 — abort LLM compaction on a TRANSIENT failure (Hermes parity).

POLYROB previously responded to ANY summarizer failure by building a lossy static
fallback summary (or, if empty, an emergency prune) — permanently discarding context.
A transient hiccup (rate-limit / auth blip / connection drop) should NOT cause
permanent lossy compaction: abort, keep the FULL context, and retry next step. The
>=95% mechanical emergency prune remains the overflow backstop, so aborting is safe.

Non-transient failures still take the deterministic static fallback (retrying won't
help).
"""
import logging

import pytest

from core.exceptions import LLMRateLimitError
from modules.llm.messages import AIMessage, HumanMessage
from agents.task.agent.messages.compactor import CompactorMixin
from agents.task.agent.message_manager.views import (
    ManagedMessage, MessageHistory, MessageMetadata,
)


class _RaisingLLM:
    def __init__(self, exc):
        self.exc = exc

    async def ainvoke(self, messages):
        raise self.exc


class _Harness(CompactorMixin):
    def __init__(self, llm):
        self.logger = logging.getLogger("test_compaction_transient_abort")
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


def _fill(h, n=30, size=4000):
    for i in range(n):
        h._add_message_with_tokens(HumanMessage(content=("x" * size) + f"-m{i}"))


@pytest.mark.asyncio
async def test_transient_failure_aborts_and_keeps_full_context():
    h = _Harness(_RaisingLLM(LLMRateLimitError("429 slow down")))
    _fill(h)
    before = len(h.history.messages)
    result = await h.llm_compact_history()
    assert result is False          # aborted, not "performed"
    assert len(h.history.messages) == before  # full context preserved


@pytest.mark.asyncio
async def test_non_transient_failure_uses_static_fallback():
    h = _Harness(_RaisingLLM(RuntimeError("malformed prompt")))
    _fill(h)
    before = len(h.history.messages)
    result = await h.llm_compact_history()
    assert result is True                       # static fallback performed
    assert len(h.history.messages) < before     # middle was compacted
