"""G-26 reachability fix (Task 5c follow-up): `_bill_llm_response`
(agents/task/agent/core/next_action_internal.py ~:146) is one of the two
billing call sites named in the fix -- verify it extracts the provider's
stable response id and threads it through to
`usage_tracker.record_llm_usage(request_id=...)`.

The OTHER call site (the inline native-tool-calling path, ~:614) shares the
exact same `extract_stable_request_id(response, provider)` wiring one-liner, verified directly in
test_stable_request_id.py; that logic is not duplicated here.
"""
import logging
import types

import pytest

from agents.task.agent.core.next_action_internal import NextActionInternalMixin


class _Agent(NextActionInternalMixin):
    """Bare host object exercising only `_bill_llm_response`."""

    def __init__(self, usage_tracker, llm):
        self.usage_tracker = usage_tracker
        self.user_id = "u1"
        self.session_id = "s1"
        self.agent_id = "a1"
        self.model_name = "claude-x"
        self.llm_provider = "anthropic"
        self.llm = llm
        self.telemetry_manager = None
        self.logger = logging.getLogger("test-bill-llm-response")


class _CapturingTracker:
    def __init__(self):
        self.calls = []

    async def record_llm_usage(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            costs=types.SimpleNamespace(api_cost_usd=0.01, user_cost_usd=0.02, credits_charged=2)
        )


def _response(prompt_tokens=100, completion_tokens=50, **marks):
    return types.SimpleNamespace(
        content="ok", **marks,
        usage_metadata={
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


@pytest.mark.asyncio
async def test_bill_llm_response_extracts_and_passes_provider_response_id():
    """A message that carries its provider id, and a spy on record_llm_usage's request_id."""
    tracker = _CapturingTracker()
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(
        last_response=types.SimpleNamespace(id="msg_of_another_session")))
    agent = _Agent(tracker, llm)

    response = _response(_polyrob_provider_response_id="msg_abc")
    await agent._bill_llm_response(response, llm_duration=1.0, provider="anthropic")

    assert len(tracker.calls) == 1
    assert tracker.calls[0]["request_id"] == "resp:anthropic:msg_abc"


@pytest.mark.asyncio
async def test_bill_llm_response_passes_none_when_no_provider_id_available():
    tracker = _CapturingTracker()
    llm = types.SimpleNamespace(_client=types.SimpleNamespace(last_response=None))
    agent = _Agent(tracker, llm)

    response = _response()
    await agent._bill_llm_response(response, llm_duration=1.0, provider="gemini")

    assert len(tracker.calls) == 1
    assert tracker.calls[0]["request_id"] is None


@pytest.mark.asyncio
async def test_bill_llm_response_distinct_responses_get_distinct_ids():
    """Two genuinely different completions (different provider ids) must
    never share a request_id."""
    tracker = _CapturingTracker()
    agent = _Agent(tracker, types.SimpleNamespace())

    await agent._bill_llm_response(_response(_polyrob_provider_response_id="msg_first"),
                                   llm_duration=1.0, provider="anthropic")
    await agent._bill_llm_response(_response(_polyrob_provider_response_id="msg_second"),
                                   llm_duration=1.0, provider="anthropic")

    ids = [c["request_id"] for c in tracker.calls]
    assert ids == ["resp:anthropic:msg_first", "resp:anthropic:msg_second"]
    assert ids[0] != ids[1]
