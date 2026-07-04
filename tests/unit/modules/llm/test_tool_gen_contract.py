"""Task 2.3 — single tool-generation return contract.

Contract: every provider's generate_agent_response() returns a 3-tuple
    (content: str|None, tool_calls: list, usage: dict)

RED paths (current):
- DeepSeek returns bare str when no tool_calls → not a tuple
- Anthropic _generate_with_tools error path returns 2-tuple (content, [])

GREEN after normalization + adapters unpack rejects non-3-tuple.
"""
import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helper: run coroutine synchronously (always creates a fresh event loop)
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DeepSeek: no-tool-calls path must return 3-tuple
# ---------------------------------------------------------------------------
class TestDeepSeekContractNoTools:
    """generate_agent_response returns 3-tuple even when tool_calls is empty."""

    def _build_client(self):
        """Build a DeepSeekClient with all external I/O mocked."""
        from modules.llm.deepseek_client import DeepSeekClient

        client = object.__new__(DeepSeekClient)
        client.model_type = "deepseek-chat"
        client.logger = logging.getLogger("test.deepseek")
        client.last_response = None
        # Stub _generate_with_tools to return a 3-tuple (the internal method already does)
        # We test generate_agent_response which previously stripped to bare-str
        client._generate_with_tools = AsyncMock(return_value=(
            "Hello world",  # content
            [],             # tool_calls (empty → was the bare-str trigger)
            {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        ))
        return client

    def test_returns_3tuple_when_no_tool_calls(self):
        """generate_agent_response MUST return 3-tuple even with empty tool_calls."""
        client = self._build_client()
        result = _run(client.generate_agent_response(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        ))
        assert isinstance(result, tuple), (
            f"Expected 3-tuple but got {type(result).__name__}: {result!r}"
        )
        assert len(result) == 3, f"Expected 3-tuple but got {len(result)}-tuple: {result!r}"
        content, tool_calls, usage = result
        assert content == "Hello world"
        assert tool_calls == []
        assert isinstance(usage, dict)

    def test_returns_3tuple_when_tool_calls_present(self):
        """generate_agent_response returns 3-tuple with tool calls too."""
        from modules.llm.deepseek_client import DeepSeekClient

        client = object.__new__(DeepSeekClient)
        client.model_type = "deepseek-chat"
        client.logger = logging.getLogger("test.deepseek")
        client.last_response = None
        tc = [{'id': 'call_1', 'type': 'function', 'function': {'name': 'foo', 'arguments': '{}'}}]
        client._generate_with_tools = AsyncMock(return_value=(
            None,
            tc,
            {'prompt_tokens': 20, 'completion_tokens': 8, 'total_tokens': 28}
        ))

        result = _run(client.generate_agent_response(
            messages=[{"role": "user", "content": "do something"}],
            tools=[{'name': 'foo'}],
        ))
        assert isinstance(result, tuple) and len(result) == 3
        content, tool_calls, usage = result
        assert tool_calls == tc
        assert isinstance(usage, dict)


# ---------------------------------------------------------------------------
# Anthropic: generate_agent_response unpacks 3-tuple (dead 2-tuple branch removed)
# ---------------------------------------------------------------------------
class TestAnthropicGenerateAgentResponse:
    """generate_agent_response must unpack the 3-tuple from _generate_with_tools
    directly — the dead 2-tuple/bare-str branches were removed in Task 2.3 fix."""

    def _build_client(self):
        from modules.llm.anthropic_client import AnthropicClient
        client = object.__new__(AnthropicClient)
        client.model_type = "claude-opus-4-5"
        client.logger = logging.getLogger("test.anthropic_gar")
        client.last_response = None
        client._client = MagicMock()
        return client

    def test_3tuple_passthrough_no_tool_calls(self):
        """generate_agent_response passes through the 3-tuple when no tool_calls."""
        client = self._build_client()
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        client._generate_with_tools = AsyncMock(return_value=("Hello", [], usage))

        result = _run(client.generate_agent_response(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        ))
        assert isinstance(result, tuple) and len(result) == 3
        content, tool_calls, usage_out = result
        assert content == "Hello"
        assert tool_calls == []
        assert usage_out == usage

    def test_3tuple_passthrough_with_tool_calls(self):
        """generate_agent_response passes through the 3-tuple when tool_calls present."""
        client = self._build_client()
        tc = [{'id': 'call_x', 'type': 'function', 'function': {'name': 'bar', 'arguments': '{}'}}]
        usage = {'prompt_tokens': 20, 'completion_tokens': 8, 'total_tokens': 28}
        client._generate_with_tools = AsyncMock(return_value=(None, tc, usage))

        result = _run(client.generate_agent_response(
            messages=[{"role": "user", "content": "do it"}],
            tools=[{'name': 'bar'}],
        ))
        assert isinstance(result, tuple) and len(result) == 3
        content, tool_calls, usage_out = result
        assert content is None
        assert tool_calls == tc
        assert usage_out == usage


# ---------------------------------------------------------------------------
# Anthropic: error path must return 3-tuple
# ---------------------------------------------------------------------------
class TestAnthropicContractErrorPath:
    """_generate_with_tools error path must return 3-tuple, not 2-tuple."""

    def _build_client(self):
        """Build a minimal AnthropicClient with stubbed internals."""
        from modules.llm.anthropic_client import AnthropicClient

        client = object.__new__(AnthropicClient)
        client.model_type = "claude-opus-4-5"
        client.logger = logging.getLogger("test.anthropic")
        client.last_response = None
        # Stub the internal Anthropic SDK client
        client._client = MagicMock()
        return client

    def test_error_path_returns_3tuple(self):
        """When _generate_with_tools raises and falls back to _generate,
        it must return a 3-tuple (content, [], usage_dict)."""
        from modules.llm.anthropic_client import AnthropicClient

        client = self._build_client()
        # _generate is the fallback when tool-calling errors
        client._generate = AsyncMock(return_value="Fallback text")
        client._extract_usage_and_capture_telemetry = MagicMock()

        # Trigger the error path by making the API call raise inside _generate_with_tools
        # We patch messages.create to raise
        client._client.messages.create = MagicMock(side_effect=Exception("API error"))
        client._client.messages.stream = MagicMock(side_effect=Exception("API error"))

        # _generate_with_tools should catch the error and call _generate, returning fallback
        result = _run(client._generate_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "do_thing"}],
            system="sys",
            temperature=0.0,
            max_tokens=1024,
        ))

        assert isinstance(result, tuple), (
            f"Expected 3-tuple on error path but got {type(result).__name__}: {result!r}"
        )
        assert len(result) == 3, (
            f"Expected 3-tuple on error path but got {len(result)}-tuple: {result!r}"
        )
        content, tool_calls, usage = result
        assert content == "Fallback text"
        assert tool_calls == []
        assert isinstance(usage, dict)


# ---------------------------------------------------------------------------
# Adapters: single unpack rejects non-3-tuple
# ---------------------------------------------------------------------------
class TestAdaptersUnpackGuard:
    """adapters.py must raise clearly on a non-3-tuple instead of silently
    branching into old code paths."""

    def _adapter(self):
        from modules.llm.adapters import LLMClientAdapter
        a = object.__new__(LLMClientAdapter)
        a._logger = logging.getLogger("test.adapter")
        a._client = MagicMock()
        a._client.model_type = "gpt-4o"
        return a

    def test_bare_string_rejected(self):
        """A bare string from a provider must raise, not silently pass."""
        a = self._adapter()
        with pytest.raises(Exception, match=r"3-tuple|contract|tuple"):
            a._unpack_tool_gen_result("bare string")

    def test_2tuple_rejected(self):
        """A 2-tuple must raise."""
        a = self._adapter()
        with pytest.raises(Exception, match=r"3-tuple|contract|tuple"):
            a._unpack_tool_gen_result(("content", []))

    def test_3tuple_accepted(self):
        """A 3-tuple must be accepted and unpacked correctly."""
        a = self._adapter()
        usage = {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}
        content, tool_calls, usage_out = a._unpack_tool_gen_result(
            ("hello", [], usage)
        )
        assert content == "hello"
        assert tool_calls == []
        assert usage_out == usage
