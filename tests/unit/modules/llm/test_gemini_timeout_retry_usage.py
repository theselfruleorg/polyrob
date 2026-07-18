"""G-25 (Task 5b): Gemini timeout-retry fallback must carry REAL non-None
usage end-to-end, not just satisfy the 3-tuple shape.

Complements the existing structural regression guard
(tests/unit/modules/llm/test_gemini_tuple_contract.py -- which only asserts
no 2-tuple `return` statement exists in the source) with a runtime test that
mocks the Gemini SDK: the with-tools call times out, the retry-without-tools
call succeeds with a distinguishable usage payload, and the tuple
`_generate_with_tools` hands back to the caller must carry those exact real
numbers through `_extract_usage_data` -- not the `ValueError` that a stray
2-tuple used to raise in `_unpack_tool_gen_result`, and not a silently
dropped/zeroed usage dict.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from modules.llm.gemini_client import GeminiClient


@pytest.fixture
def mock_config():
    config = Mock()
    config.get_llm_config.return_value = {
        'gemini': {
            'model': 'gemini-2.0-flash',  # NOT a gemini-3 model -> no ChatSession path
            'api_key': 'test_key_12345',
        }
    }
    return config


@pytest.fixture
def gemini_client(mock_config):
    with patch('modules.llm.gemini_client.genai'):
        client = GeminiClient(mock_config)
        client._initialized = True
        return client


def _model_factory(success_response, calls):
    """genai.GenerativeModel(...) side_effect: the WITH-tools construction
    (called with a `tools=` kwarg) returns a model whose generate call times
    out; the WITHOUT-tools retry construction (no `tools=` kwarg) returns a
    model whose generate call succeeds with `success_response`."""

    def _factory(*args, **kwargs):
        model = Mock()
        if 'tools' in kwargs:
            calls['with_tools_constructed'] += 1

            async def _timeout(*a, **kw):
                calls['with_tools_called'] += 1
                raise asyncio.TimeoutError()

            model.generate_content_async = _timeout
        else:
            calls['without_tools_constructed'] += 1

            async def _succeed(*a, **kw):
                calls['without_tools_called'] += 1
                return success_response

            model.generate_content_async = _succeed
        return model

    return _factory


@pytest.mark.asyncio
async def test_timeout_retry_carries_real_usage_end_to_end(gemini_client):
    usage_metadata = SimpleNamespace(
        prompt_token_count=2222,
        candidates_token_count=333,
        total_token_count=2555,
        cached_content_token_count=0,
    )
    success_response = SimpleNamespace(text="fallback response text", usage_metadata=usage_metadata)

    calls = {
        'with_tools_constructed': 0, 'with_tools_called': 0,
        'without_tools_constructed': 0, 'without_tools_called': 0,
    }

    with patch('modules.llm.gemini_client.genai.GenerativeModel',
               side_effect=_model_factory(success_response, calls)):
        # Force a fast "timeout" instead of waiting out the real
        # DEFAULT_REQUEST_TIMEOUT: the mocked coroutine raises
        # asyncio.TimeoutError() directly, which asyncio.wait_for propagates
        # unchanged -- exactly what a real wait_for timeout looks like from
        # the caller's perspective.
        content, tool_calls, usage = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "do_x", "description": "d",
                   "parameters": {"type": "object", "properties": {}}}],
            system="sys", max_tokens=256,
        )

    assert calls['with_tools_called'] == 1
    assert calls['without_tools_called'] == 1
    assert content == "fallback response text"
    assert tool_calls == []

    # This is exactly the case the old code got wrong: a 2-tuple return
    # raised ValueError in _unpack_tool_gen_result before usage ever reached
    # the caller. Now it must be the REAL usage from the retry response.
    assert usage["prompt_tokens"] == 2222
    assert usage["completion_tokens"] == 333
    assert usage["total_tokens"] == 2555
    assert usage["prompt_tokens"] is not None
    assert usage["completion_tokens"] is not None


@pytest.mark.asyncio
async def test_unpack_tool_gen_result_accepts_the_fallback_tuple(gemini_client):
    """The consumer-side contract this bug broke: `_unpack_tool_gen_result`
    (agents/task) requires a strict 3-tuple and ValueErrors on anything
    else. Prove the fallback path now satisfies that contract directly,
    without needing to stand up the full agent stack."""
    usage_metadata = SimpleNamespace(
        prompt_token_count=10, candidates_token_count=5,
        total_token_count=15, cached_content_token_count=0,
    )
    success_response = SimpleNamespace(text="ok", usage_metadata=usage_metadata)
    calls = {
        'with_tools_constructed': 0, 'with_tools_called': 0,
        'without_tools_constructed': 0, 'without_tools_called': 0,
    }

    with patch('modules.llm.gemini_client.genai.GenerativeModel',
               side_effect=_model_factory(success_response, calls)):
        result = await gemini_client._generate_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "do_x", "description": "d",
                   "parameters": {"type": "object", "properties": {}}}],
            system="sys", max_tokens=256,
        )

    # The bug was literally: this used to be a 2-tuple.
    assert isinstance(result, tuple) and len(result) == 3
    content, tool_calls, usage = result
    assert isinstance(usage, dict)
    assert usage.get("prompt_tokens") == 10
