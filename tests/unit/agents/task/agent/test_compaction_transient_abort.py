"""Phase 0.3 — abort LLM compaction on a TRANSIENT failure (reference parity).

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

from core.exceptions import LLMConnectionError, LLMRateLimitError
from modules.llm.call_scope import anchored_input_tokens, call_scope, call_timeout_param, scoped_max_retries
from modules.llm.messages import AIMessage, HumanMessage
from agents.task.agent.messages.compactor import CompactorMixin
from agents.task.agent.message_manager.views import (
    ManagedMessage, MessageHistory, MessageMetadata,
)


class _RaisingLLM:
    def __init__(self, exc):
        self.exc = exc

    async def ainvoke(self, messages, **kwargs):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [TimeoutError(), LLMConnectionError("OpenRouter: Request timed out.")])
async def test_a_summary_timeout_is_reported_to_the_step(exc):
    h = _Harness(_RaisingLLM(exc))
    _fill(h)
    assert await h.llm_compact_history() is False
    assert h.last_summary_report == {"timed_out": True}


@pytest.mark.asyncio
async def test_another_transient_failure_is_not_a_timeout():
    h = _Harness(_RaisingLLM(LLMRateLimitError("429 slow down")))
    h.last_summary_report = None
    _fill(h)
    assert await h.llm_compact_history() is False
    assert h.last_summary_report is None


class _ScopeReader:
    async def ainvoke(self, messages, **kwargs):
        self.seen = (call_timeout_param(), anchored_input_tokens(), scoped_max_retries())
        return AIMessage(content="ok")


@pytest.mark.asyncio
async def test_a_summary_call_owns_its_budget_with_no_sdk_retry_and_no_step_anchor():
    llm = _ScopeReader()
    h = _Harness(llm)
    h.calculate_llm_timeout = lambda: 240.0
    with call_scope(timeout=30.0, input_tokens=9000):
        await h._bounded_summary_call(llm, [HumanMessage(content="x")], tools=None)
        assert anchored_input_tokens() == 9000
    assert llm.seen == ({"timeout": 240.0}, None, 0)
    assert call_timeout_param() == {}
