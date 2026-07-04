"""G1/G2 (telemetry audit 2026-07-04): every finalized LLM response must be billed.

Billing lived inline in ONLY the native-tool-calling branch, so the structured-output
and plain fallback completions consumed tokens with no billing record (G2). The fix
extracts `_bill_llm_response`, called from every response-producing path. This tests
the helper: it bills the passed response's tokens, is idempotent per response object
(no double-charge), and no-ops safely without a usage_tracker.
"""
import logging
from types import SimpleNamespace

import pytest

from agents.task.agent.core.next_action_internal import NextActionInternalMixin


class _FakeTracker:
    def __init__(self):
        self.calls = []

    async def record_llm_usage(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            costs=SimpleNamespace(api_cost_usd=0.1, user_cost_usd=0.2, credits_charged=1)
        )


class _Resp:
    def __init__(self):
        self.usage_metadata = {
            "input_tokens": 10000,
            "output_tokens": 5000,
            "total_tokens": 15000,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 4000,
        }


def _mixin(tracker):
    m = NextActionInternalMixin.__new__(NextActionInternalMixin)
    m.logger = logging.getLogger("bill-test")
    m.usage_tracker = tracker
    m.user_id = "u1"
    m.session_id = "s1"
    m.agent_id = "agent_s1"
    m.model_name = "claude-sonnet-4-5"
    m.llm_provider = "anthropic"
    m.telemetry_manager = None
    m.state = SimpleNamespace(n_steps=1)
    return m


@pytest.mark.asyncio
async def test_bills_a_response_with_all_token_fields():
    tracker = _FakeTracker()
    m = _mixin(tracker)

    await m._bill_llm_response(_Resp(), llm_duration=1.5, provider="anthropic")

    assert len(tracker.calls) == 1
    call = tracker.calls[0]
    assert call["input_tokens"] == 10000
    assert call["output_tokens"] == 5000
    assert call["cached_tokens"] == 2000
    assert call["cache_creation_tokens"] == 4000
    assert call["success"] is True


@pytest.mark.asyncio
async def test_idempotent_per_response_object():
    """Billing the SAME response twice must charge once (no double-bill)."""
    tracker = _FakeTracker()
    m = _mixin(tracker)
    resp = _Resp()

    await m._bill_llm_response(resp, llm_duration=1.0)
    await m._bill_llm_response(resp, llm_duration=1.0)

    assert len(tracker.calls) == 1


@pytest.mark.asyncio
async def test_distinct_responses_each_billed():
    """A fallback response (distinct object) is billed even after a native one — G2."""
    tracker = _FakeTracker()
    m = _mixin(tracker)

    await m._bill_llm_response(_Resp(), llm_duration=1.0)   # native
    await m._bill_llm_response(_Resp(), llm_duration=1.0)   # fallback

    assert len(tracker.calls) == 2


@pytest.mark.asyncio
async def test_no_tracker_does_not_raise():
    m = _mixin(tracker=None)
    m.usage_tracker = None
    # Must not raise even though there's no biller.
    await m._bill_llm_response(_Resp(), llm_duration=1.0)


@pytest.mark.asyncio
async def test_none_response_noop():
    tracker = _FakeTracker()
    m = _mixin(tracker)
    await m._bill_llm_response(None, llm_duration=1.0)
    assert tracker.calls == []
