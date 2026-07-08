"""P1-9 (intelligence-polish plan 2026-07-07): extended thinking must NOT be enabled
on a tool-calling request.

Anthropic requires the assistant's thinking block(s) (with signature) to precede
tool_use when tool_results are sent back with thinking on. This client discards
thinking blocks and never replays them, so enabling thinking on a tool loop 400s at
step 2. Until block-replay lands, thinking is refused when tools are present (the flag
is default-OFF; this makes turning it on safe). No-tool calls still get thinking.

The SDK mock records EACH call's params and always raises; the method's own error
handling then falls back to a non-tool path, so we assert on the FIRST capture (the
primary _generate_with_tools request) rather than the muddied final state.
"""
import logging

import pytest

from modules.llm.anthropic_client import AnthropicClient


def _client(calls):
    c = object.__new__(AnthropicClient)
    c._initialized = True
    c.model_type = "claude-sonnet-4-5"
    c.temperature = 0.7
    c.logger = logging.getLogger("p19")

    class _Messages:
        async def create(self, **params):
            calls.append(dict(params))
            raise RuntimeError("stop after capture")

        def stream(self, **params):
            calls.append(dict(params))
            raise RuntimeError("stop after capture")

    class _SDK:
        messages = _Messages()

    c._client = _SDK()
    return c


async def _run(client, tools):
    try:
        await client._generate_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools, system="sys", max_tokens=1024,
        )
    except Exception:
        pass


@pytest.mark.asyncio
async def test_thinking_disabled_when_tools_present(monkeypatch):
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    calls = []
    c = _client(calls)
    tools = [{"name": "do_x", "input_schema": {"type": "object", "properties": {}}}]
    await _run(c, tools)
    assert calls, "the primary request should have been attempted"
    assert "thinking" not in calls[0], "thinking must be OFF on a tool-calling request"


@pytest.mark.asyncio
async def test_thinking_enabled_when_no_tools(monkeypatch):
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    calls = []
    c = _client(calls)
    await _run(c, [])  # no tools → thinking may be enabled
    assert calls
    assert "thinking" in calls[0], "thinking should be enabled on a no-tool call"
    assert calls[0].get("temperature") == 1  # required by the API when thinking is on


@pytest.mark.asyncio
async def test_thinking_off_by_default_even_without_tools(monkeypatch):
    monkeypatch.delenv("THINKING_CONFIG_ENABLED", raising=False)
    calls = []
    c = _client(calls)
    await _run(c, [])
    assert calls
    assert "thinking" not in calls[0]
