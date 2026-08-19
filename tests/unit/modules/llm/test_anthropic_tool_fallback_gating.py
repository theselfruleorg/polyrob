"""2026-08-16 storm fix: _generate_with_tools must NOT retry without tools on
rate-limit / billing / connection errors.

Live evidence (prod journal 2026-08-13..16): every z.ai 429 in the tool path
triggered an immediate second non-tool call (856 blind fallback calls in 4
days — doubled hammering on an already rate-limited account), and whenever
that second call succeeded it returned ZERO tool_calls, producing the
"Model output has empty action list" / thinking-loop CRITICAL storms.

The fallback is now gated: only a request-shape problem (LLMInvalidRequestError
per translate_llm_error) may drop the tools block; everything else re-raises so
the adapter classifies it and the agent runs provider fallback.
"""
import logging
from types import SimpleNamespace

import pytest

from modules.llm.anthropic_client import AnthropicClient


def _make_client(primary_error: str):
    """Minimal instantiation (object.__new__, no API key) with an SDK stub
    whose first messages.create raises *primary_error* and whose second call
    would succeed — so a gated re-raise shows up as calls == 1."""
    c = object.__new__(AnthropicClient)
    c._initialized = True
    c.model_type = "claude-sonnet-4-5"
    c.temperature = 0.7
    c.logger = logging.getLogger("anthropic-fallback-gating")

    calls = {"count": 0}

    class _Messages:
        async def create(self, **params):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError(primary_error)
            text_block = SimpleNamespace(type="text", text="fallback response")
            usage = SimpleNamespace(
                input_tokens=1, output_tokens=1,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            )
            return SimpleNamespace(content=[text_block], usage=usage)

        def stream(self, **params):
            raise AssertionError("streaming path not expected here")

    class _SDK:
        messages = _Messages()

    c._client = _SDK()
    return c, calls


TOOLS = [{"name": "do_x", "input_schema": {"type": "object", "properties": {}}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_text", [
    # transient rate limit — retrying without tools just doubles the hammering
    "Error code: 429 - {'type': 'rate_limit_error', 'message': 'concurrency limit'}",
    # plan-quota death dressed as a 429 (z.ai 1310)
    ("Error code: 429 - {'type': 'rate_limit_error', 'code': '1310', 'message': "
     "'[1310][Weekly/Monthly Limit Exhausted. Your limit will reset at "
     "2026-08-18 18:01:49]'}"),
    # billing death
    "Error code: 402 - insufficient credits",
    # network
    "connection reset by peer",
])
async def test_non_request_errors_reraise_without_fallback(error_text):
    client, calls = _make_client(error_text)
    with pytest.raises(RuntimeError):
        await client._generate_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS, system="sys", max_tokens=1024,
        )
    assert calls["count"] == 1, (
        f"{error_text!r} must re-raise for provider fallback, not retry "
        f"without tools (made {calls['count']} calls)")


@pytest.mark.asyncio
async def test_invalid_request_still_falls_back_to_non_tool():
    client, calls = _make_client(
        "Error code: 400 - invalid_request_error: tools.0.input_schema is malformed")
    content, tool_calls, usage = await client._generate_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS, system="sys", max_tokens=1024,
    )
    assert calls["count"] == 2, "a request-shape error keeps the non-tool fallback"
    assert content == "fallback response"
    assert tool_calls == []
