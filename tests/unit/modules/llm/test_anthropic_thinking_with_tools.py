"""A budget row (older than 4.6) with THINKING_CONFIG_ENABLED gets its thinking block on a
tool-calling request too: the client replays the blocks. The real API takes no temperature.

The SDK mock records EACH call's params and always raises, so we assert on the FIRST capture.
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
async def test_a_budget_row_thinks_when_tools_are_present(monkeypatch):
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    calls = []
    c = _client(calls)
    tools = [{"name": "do_x", "input_schema": {"type": "object", "properties": {}}}]
    await _run(c, tools)
    assert calls, "the primary request should have been attempted"
    assert calls[0]["thinking"]["type"] == "enabled"
    assert "temperature" not in calls[0]


@pytest.mark.asyncio
async def test_thinking_enabled_when_no_tools(monkeypatch):
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    calls = []
    c = _client(calls)
    await _run(c, [])  # no tools → thinking may be enabled
    assert calls
    assert "thinking" in calls[0], "thinking should be enabled on a no-tool call"
    assert "temperature" not in calls[0]


@pytest.mark.asyncio
async def test_thinking_off_by_default_even_without_tools(monkeypatch):
    monkeypatch.delenv("THINKING_CONFIG_ENABLED", raising=False)
    calls = []
    c = _client(calls)
    await _run(c, [])
    assert calls
    assert "thinking" not in calls[0]
