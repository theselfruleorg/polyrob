"""The OpenAI wire (106 L14) and the token estimate (L17): no raw metadata, no accidental
``store``, a validation call the model accepts, no tokenizer on the event loop."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.llm import token_counter
from modules.llm.openai_client import OpenAIClient
from modules.llm.token_counter import count_messages_tokens


def _openai():
    client = OpenAIClient.__new__(OpenAIClient)
    client.logger = logging.getLogger("test.openai")
    client.model_type = "gpt-5"
    client.temperature = 0.7
    client.max_tokens = 1000
    client._initialized = True
    answer = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(choices=[answer], usage=None, error=None)))))
    return client


@pytest.mark.asyncio
async def test_metadata_and_store_never_reach_the_wire():
    client = _openai()
    await client.generate_response(prompt="hi", metadata={"user_id": "u", "note": "x"}, store=True)
    sent = client._client.chat.completions.create.await_args.kwargs
    assert "metadata" not in sent and "store" not in sent


@pytest.mark.asyncio
async def test_the_validation_call_uses_the_parameter_gpt5_accepts():
    client = _openai()
    await client._make_validation_request()
    sent = client._client.chat.completions.create.await_args.kwargs
    assert sent["max_completion_tokens"] == 1 and "max_tokens" not in sent


def test_an_id_with_no_row_loads_no_tokenizer(monkeypatch):
    tiktoken = MagicMock()
    monkeypatch.setattr(token_counter, "tiktoken", tiktoken, raising=False)
    monkeypatch.setattr(token_counter, "TIKTOKEN_AVAILABLE", True)
    assert token_counter.EncoderCache()._create_encoder("vendor/no-such-model-xyz") is None
    assert not tiktoken.mock_calls


def test_the_estimate_loads_no_tokenizer(monkeypatch):
    def _refuse(*_a):
        raise AssertionError("no encoder on this path")
    monkeypatch.setattr(token_counter.EncoderCache, "get_encoder", _refuse)
    messages = [{"role": "user", "content": "x" * 4000}]
    assert count_messages_tokens(messages, "gpt-5", encode=False) > 900


def test_the_output_clamp_estimates_with_no_tokenizer(monkeypatch):
    from modules.llm.llm_client import LLMClient

    def _refuse(*_a):
        raise AssertionError("no encoder on this path")
    monkeypatch.setattr(token_counter.EncoderCache, "get_encoder", _refuse)
    stub = SimpleNamespace(max_tokens=100, model_type="gpt-5", logger=logging.getLogger("t"),
                           get_context_window=lambda: 100000,
                           get_max_completion_tokens=lambda: 100)
    assert LLMClient._adjust_max_tokens(stub, [{"role": "user", "content": "hi"}]) == 100
