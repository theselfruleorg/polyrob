"""OpenAI Responses transport (proposal 024 L2 — the third wire format).

Subscription seats served through OpenAI's Codex backend speak Responses and
nothing else, so before this a connected ChatGPT plan stored a credential that
inference could never use.

Implemented against the published Responses wire contract, with attention to
the format's sharp edges. Each test below pins one of those edges — they are
cheap to get wrong and expensive to diagnose, because most of them fail SILENTLY
rather than raising.
"""
from __future__ import annotations

import json

import pytest

from modules.llm.responses_client import (
    from_responses_output,
    to_responses_input,
    to_responses_tools,
)


class _Obj:
    """Attribute-style stand-in for the SDK's response objects."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# Tool schemas — the silent failure
# ---------------------------------------------------------------------------
class TestToolSchemas:
    def test_schema_is_flat_not_nested_under_function(self):
        """THE silent failure of this transport. Chat-completions nests under
        `function`; Responses wants name/description/parameters at the top
        level. The nested shape is ACCEPTED and then ignored, so the model
        simply never calls a tool — no error anywhere."""
        out = to_responses_tools([{
            "type": "function",
            "function": {"name": "read_file", "description": "Read a file",
                         "parameters": {"type": "object",
                                        "properties": {"path": {"type": "string"}}}},
        }])
        assert out == [{
            "type": "function", "name": "read_file", "description": "Read a file",
            "strict": False,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }]
        assert "function" not in out[0]

    def test_strict_is_false(self):
        """strict=True demands a fully-closed JSON schema; our tool schemas are
        not authored to that standard and the request would be rejected."""
        out = to_responses_tools([{"function": {"name": "x", "parameters": {}}}])
        assert out[0]["strict"] is False

    def test_unnamed_and_empty_tools_are_dropped(self):
        assert to_responses_tools([]) is None
        assert to_responses_tools(None) is None
        assert to_responses_tools([{"function": {"description": "no name"}}]) is None


# ---------------------------------------------------------------------------
# Input conversion
# ---------------------------------------------------------------------------
class TestInputConversion:
    def test_system_becomes_instructions_not_a_role(self):
        """`system` is not a role in this API."""
        items, instructions = to_responses_input([
            {"role": "system", "content": "You are NOVA."},
            {"role": "user", "content": "hi"},
        ])
        assert instructions == "You are NOVA."
        assert all(i.get("role") != "system" for i in items)

    def test_text_part_type_follows_the_role(self):
        """The API rejects input_text inside an assistant message and
        output_text inside a user message."""
        items, _ = to_responses_input([
            {"role": "user", "content": "ask"},
            {"role": "assistant", "content": "answer"},
        ])
        assert items[0]["content"][0]["type"] == "input_text"
        assert items[1]["content"][0]["type"] == "output_text"

    def test_tool_calls_and_results_are_top_level_items(self):
        """They are NOT fields on a message in this format."""
        items, _ = to_responses_input([
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "fc_1", "type": "function",
                 "function": {"name": "ls", "arguments": '{"a":1}'}}]},
            {"role": "tool", "tool_call_id": "fc_1", "content": "README.md"},
        ])
        call = next(i for i in items if i.get("type") == "function_call")
        assert (call["call_id"], call["name"], call["arguments"]) == ("fc_1", "ls", '{"a":1}')
        result = next(i for i in items if i.get("type") == "function_call_output")
        assert (result["call_id"], result["output"]) == ("fc_1", "README.md")

    def test_oversized_item_ids_are_dropped(self):
        """An id over 64 chars is a non-retryable 400, and server-assigned ids
        routinely exceed it — echoing one back kills the whole request."""
        items, _ = to_responses_input([
            {"role": "assistant", "content": "x", "id": "m" * 200},
            {"role": "assistant", "content": "y", "id": "short"},
        ])
        assert "id" not in items[0]
        assert items[1]["id"] == "short"

    def test_dict_arguments_are_serialized(self):
        items, _ = to_responses_input([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c", "function": {"name": "f", "arguments": {"k": "v"}}}]},
        ])
        call = next(i for i in items if i.get("type") == "function_call")
        assert json.loads(call["arguments"]) == {"k": "v"}


# ---------------------------------------------------------------------------
# Output conversion
# ---------------------------------------------------------------------------
class TestOutputConversion:
    def test_text_and_tool_calls_come_back_in_chat_completions_shape(self):
        """Everything downstream (ToolCallBuilder -> Tracker -> Registry)
        consumes the nested `function` shape."""
        resp = _Obj(output=[
            _Obj(type="message", content=[_Obj(type="output_text", text="Reading.")]),
            _Obj(type="function_call", call_id="fc_a", name="read_file",
                 arguments='{"path":"README.md"}'),
        ])
        text, calls = from_responses_output(resp)
        assert text == "Reading."
        assert calls == [{"id": "fc_a", "type": "function",
                          "function": {"name": "read_file",
                                       "arguments": '{"path":"README.md"}'}}]

    def test_reasoning_items_never_leak_into_content(self):
        """The model's scratchpad reaching history/brain-state is exactly what
        the think-scrubber exists to prevent — don't hand it over here."""
        resp = _Obj(output=[
            _Obj(type="reasoning", summary=[_Obj(type="text", text="secret plan")]),
            _Obj(type="message", content=[_Obj(type="output_text", text="Done.")]),
        ])
        text, _calls = from_responses_output(resp)
        assert text == "Done." and "secret plan" not in text

    def test_empty_output_is_not_an_error(self):
        text, calls = from_responses_output(_Obj(output=[]))
        assert (text, calls) == ("", [])
        text, calls = from_responses_output(_Obj())
        assert (text, calls) == ("", [])

    def test_a_call_without_a_name_is_dropped(self):
        _t, calls = from_responses_output(
            _Obj(output=[_Obj(type="function_call", call_id="x", arguments="{}")]))
        assert calls == []


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
class TestWiring:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
        monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
        from modules.llm.provider_spec import reset_provider_registry_cache
        reset_provider_registry_cache()
        yield
        reset_provider_registry_cache()

    def test_responses_transport_now_has_a_client(self):
        from modules.llm.provider_spec import Transport, generic_client_class_name
        assert generic_client_class_name(Transport.RESPONSES) == "ResponsesCompatClient"

    def test_codex_is_servable_again(self):
        """It was force-marked non-initializable while the transport had no
        client; shipping one must lift that automatically, not by hand."""
        from modules.llm.model_registry import PROVIDER_CONFIG
        from modules.llm.provider_spec import get_spec
        assert get_spec("openai-codex").initializable is True
        assert PROVIDER_CONFIG["openai-codex"].client_class_name == "ResponsesCompatClient"

    def test_usage_maps_input_output_onto_prompt_completion(self):
        """Responses names them input/output; the rest of the stack counts
        prompt/completion, and a mismatch silently zeroes token accounting."""
        from modules.llm.responses_client import _usage_from
        usage = _usage_from(_Obj(usage=_Obj(input_tokens=120, output_tokens=35,
                                            total_tokens=155)))
        assert usage["prompt_tokens"] == 120
        assert usage["completion_tokens"] == 35
        assert usage["total_tokens"] == 155

    def test_missing_usage_reports_unknown_not_zero(self):
        from modules.llm.responses_client import _usage_from
        assert _usage_from(_Obj()) == {"prompt_tokens": None, "completion_tokens": None,
                                       "total_tokens": None}


# ---------------------------------------------------------------------------
# The client: output cap, status, and the base generate_response signature
# ---------------------------------------------------------------------------
def _client(response):
    import logging
    from unittest.mock import AsyncMock

    from modules.llm.responses_client import ResponsesCompatClient
    client = ResponsesCompatClient.__new__(ResponsesCompatClient)
    client._PROVIDER_LABEL = "Codex"
    client.logger = logging.getLogger("test.responses")
    client.model_type = "gpt-5"
    client.max_tokens = 128000
    client.last_response = None
    client._client = _Obj(responses=_Obj(create=AsyncMock(return_value=response)))
    return client


def _done(**kw):
    return _Obj(output=[], usage=None, **kw)


class TestClient:
    @pytest.mark.asyncio
    async def test_the_shared_output_cap_binds(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "500")
        client = _client(_done())
        await client.generate_agent_response([{"role": "user", "content": "hi"}], tools=[])
        assert client._client.responses.create.await_args.kwargs["max_output_tokens"] == 500

    @pytest.mark.asyncio
    async def test_generate_response_takes_a_prompt(self):
        client = _client(_done())
        assert await client.generate_response(prompt="hi", system="be brief") == ""
        sent = client._client.responses.create.await_args.kwargs
        assert sent["instructions"] == "be brief"
        assert sent["input"][0]["content"][0]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_a_failed_response_raises_typed(self):
        from core.exceptions import LLMError
        client = _client(_done(status="failed", error=_Obj(code="server_error", message="boom")))
        with pytest.raises(LLMError, match="boom"):
            await client.generate_agent_response([{"role": "user", "content": "hi"}], tools=[])

    @pytest.mark.asyncio
    async def test_a_filtered_incomplete_response_raises(self):
        from core.exceptions import LLMRefusalError
        client = _client(_done(status="incomplete", incomplete_details=_Obj(reason="content_filter")))
        with pytest.raises(LLMRefusalError, match="content filter"):
            await client.generate_agent_response([{"role": "user", "content": "hi"}], tools=[])

    @pytest.mark.asyncio
    async def test_incomplete_at_the_cap_returns_and_reads_as_length(self):
        from core.stop_reason import normalize_stop
        from modules.llm.call_result import finish_reason_of
        response = _done(status="incomplete", incomplete_details=_Obj(reason="max_output_tokens"))
        client = _client(response)
        await client.generate_agent_response([{"role": "user", "content": "hi"}], tools=[])
        assert normalize_stop(finish_reason_of(response)) == "length"
