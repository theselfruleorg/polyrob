"""A Gemini timeout raises typed. It never retries with no tools, because a reply with no
tools reads as a turn with no action."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("google.generativeai", reason="the [gemini] extra is not installed (058 lean core) — GeminiClient imports the SDK at module load")
from core.exceptions import LLMConnectionError
from core.llm_limits import llm_request_timeout
from modules.llm.gemini_client import GeminiClient

TOOLS = [{"name": "do_x", "description": "d", "parameters": {"type": "object", "properties": {}}}]


@pytest.fixture
def gemini_client():
    config = Mock()
    config.get_llm_config.return_value = {
        'gemini': {'model': 'gemini-2.0-flash', 'api_key': 'test_key_12345'}}
    with patch('modules.llm.gemini_client.genai'):
        client = GeminiClient(config)
        client._initialized = True
        return client


@pytest.mark.asyncio
async def test_a_timeout_raises_typed_and_makes_one_call(gemini_client):
    calls = []

    def factory(*args, **kwargs):
        calls.append(kwargs)
        model = Mock()

        async def _timeout(*a, **kw):
            raise asyncio.TimeoutError()

        model.generate_content_async = _timeout
        return model

    with patch('modules.llm.gemini_client.genai.GenerativeModel', side_effect=factory):
        with pytest.raises(LLMConnectionError):
            await gemini_client._generate_with_tools(
                messages=[{"role": "user", "content": "hi"}], tools=TOOLS, system="sys", max_tokens=256)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_no_tools_path_has_a_timeout(gemini_client):
    def factory(*args, **kwargs):
        model = Mock()

        async def _hang(*a, **kw):
            await asyncio.sleep(30)

        model.generate_content_async = _hang
        return model

    with patch('modules.llm.gemini_client.genai.GenerativeModel', side_effect=factory), \
            patch.object(GeminiClient, '_call_timeout', return_value=0.01):
        with pytest.raises(LLMConnectionError):
            await gemini_client._generate(messages=[{"role": "user", "content": "hi"}], max_tokens=16)


def test_a_timeout_reads_the_step_budget(gemini_client):
    from modules.llm.call_scope import call_scope
    with call_scope(timeout=7.0):
        assert gemini_client._call_timeout() == 7.0
    assert gemini_client._call_timeout() == float(llm_request_timeout())
