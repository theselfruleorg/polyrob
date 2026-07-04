"""B5 (high) — the configured temperature/max_tokens must reach the API.

create_chat_model passes temperature into the adapter constructor, but
BaseChatModel declares no such field, so Pydantic (extra='ignore') silently
dropped it and every generation used the client's hardcoded 0.7 default —
requesting temperature=0.0 for deterministic output produced sampling at 0.7.

Fix: LLMClientAdapter captures temperature/max_tokens at construction and uses
them as the per-call fallback in _agenerate.
"""
import pytest
from unittest.mock import AsyncMock

from modules.llm.adapters import OpenAIAdapter
from modules.llm.messages import HumanMessage


class _FakeClient:
    def __init__(self):
        self.model_type = "gpt-x"
        self.last_response = None
        self.generate_response = AsyncMock(return_value="hi there")


def _adapter(**kw):
    return OpenAIAdapter(client=_FakeClient(), model_name="gpt-x", **kw)


def test_constructor_captures_temperature_and_max_tokens():
    a = _adapter(temperature=0.0, max_tokens=1234)
    assert a._default_temperature == 0.0
    assert a._default_max_tokens == 1234


@pytest.mark.asyncio
async def test_configured_temperature_reaches_client_when_call_omits_it():
    a = _adapter(temperature=0.0)
    await a._agenerate([HumanMessage(content="hello")])
    _, kwargs = a._client.generate_response.call_args
    assert kwargs.get("temperature") == 0.0


@pytest.mark.asyncio
async def test_per_call_temperature_overrides_default():
    a = _adapter(temperature=0.0)
    await a._agenerate([HumanMessage(content="hello")], temperature=0.9)
    _, kwargs = a._client.generate_response.call_args
    assert kwargs.get("temperature") == 0.9


@pytest.mark.asyncio
async def test_no_configured_temperature_leaves_it_to_client():
    a = _adapter()  # no temperature configured
    await a._agenerate([HumanMessage(content="hello")])
    _, kwargs = a._client.generate_response.call_args
    assert "temperature" not in kwargs  # client applies its own default
