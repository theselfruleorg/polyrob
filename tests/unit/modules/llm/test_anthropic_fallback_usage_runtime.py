"""G-25 (Task 5b): Anthropic tool-call fallback must carry REAL non-None
usage end-to-end -- not just structurally re-extract via
`_extract_usage_data()` (already covered by the AST-based regression guard
in test_anthropic_fallback_usage.py), but actually deliver the fallback
SDK response's real token counts through to the caller's return tuple.

Mocks the Anthropic SDK: the primary tool-call attempt raises, the fallback
(`_generate`, no tools) succeeds with a distinguishable usage payload, and
`_generate_with_tools` must hand back those exact real numbers instead of
the old fabricated all-None dict.
"""
import logging
from types import SimpleNamespace

import pytest

from modules.llm.anthropic_client import AnthropicClient


def _make_client():
    """Same minimal-instantiation pattern as
    test_anthropic_thinking_with_tools.py::_client -- `object.__new__` to
    skip __init__ (no real API key / network needed), with just enough
    attributes for `_generate_with_tools` to run."""
    c = object.__new__(AnthropicClient)
    c._initialized = True
    c.model_type = "claude-sonnet-4-5"
    c.temperature = 0.7
    c.logger = logging.getLogger("g25-anthropic-fallback-runtime")

    calls = {"count": 0}

    class _Messages:
        async def create(self, **params):
            calls["count"] += 1
            if calls["count"] == 1:
                # Primary tool-call attempt fails -> triggers the
                # error-fallback branch in _generate_with_tools.
                raise RuntimeError("simulated tool-call error")
            # Fallback call (_generate, no tools) succeeds with REAL,
            # distinguishable usage.
            text_block = SimpleNamespace(type="text", text="fallback response")
            usage = SimpleNamespace(
                input_tokens=1234,
                output_tokens=567,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
            return SimpleNamespace(content=[text_block], usage=usage)

        def stream(self, **params):
            raise AssertionError("streaming path not expected for this small max_tokens test")

    class _SDK:
        messages = _Messages()

    c._client = _SDK()
    return c, calls


@pytest.mark.asyncio
async def test_fallback_carries_real_usage_end_to_end():
    client, calls = _make_client()
    tools = [{"name": "do_x", "input_schema": {"type": "object", "properties": {}}}]

    content, tool_calls, usage = await client._generate_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools, system="sys", max_tokens=1024,
    )

    assert calls["count"] == 2, "expected exactly one failed attempt + one fallback call"
    assert tool_calls == []
    assert content == "fallback response"

    # This is exactly the case the old code fabricated as all-None,
    # silently dropping the turn's tokens from billing/compaction.
    assert usage["prompt_tokens"] == 1234
    assert usage["completion_tokens"] == 567
    assert usage["total_tokens"] == 1234 + 567
    assert usage["prompt_tokens"] is not None
    assert usage["completion_tokens"] is not None
    assert usage["total_tokens"] is not None


@pytest.mark.asyncio
async def test_fallback_usage_reaches_bill_llm_response_extraction():
    """The consumer-side contract this bug broke: `agents/task`'s
    `extract_token_usage(response, provider)` reads prompt_tokens/
    completion_tokens off whatever `_generate_with_tools` returns as usage.
    Prove the fallback's usage dict is directly usable by that extraction
    shape (dict with the standard key names), not requiring a special case."""
    client, _ = _make_client()
    tools = [{"name": "do_x", "input_schema": {"type": "object", "properties": {}}}]

    _, _, usage = await client._generate_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools, system="sys", max_tokens=1024,
    )

    # Same key names / shape as the success path's usage_data (see
    # AnthropicClient._extract_usage_data), so callers need no special-casing.
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert key in usage
    assert all(usage[k] is not None for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
