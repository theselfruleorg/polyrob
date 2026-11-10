"""The Gemini wire: finish reasons, a stopped candidate, tool names, the system rule,
thought tokens, schemas and the process environment."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("google.generativeai", reason="the [gemini] extra is not installed (058 lean core) — GeminiClient imports the SDK at module load")
import google.ai.generativelanguage as glm

from core.exceptions import LLMAuthenticationError, LLMRateLimitError, LLMResponseError
from modules.llm.gemini_client import GeminiClient
from modules.llm.gemini_parts import json_schema_to_glm, read_candidate
from modules.llm.call_result import CallFacts, finish_reason_of, output_was_truncated

FinishReason = glm.Candidate.FinishReason


@pytest.fixture
def client():
    config = Mock()
    config.get_llm_config.return_value = {
        'gemini': {'model': 'gemini-2.0-flash', 'api_key': 'test_key_12345'}}
    with patch('modules.llm.gemini_client.genai'):
        instance = GeminiClient(config)
        instance._initialized = True
        return instance


def _response(reason=FinishReason.STOP, parts=None, **usage):
    candidate = glm.Candidate(content=glm.Content(parts=parts or []), finish_reason=reason)
    return SimpleNamespace(candidates=[candidate], prompt_feedback=None,
                           usage_metadata=SimpleNamespace(**usage) if usage else None)


def _capture(response, seen=None):
    def factory(*args, **kwargs):
        if seen is not None:
            seen.update(kwargs)
        model = Mock()

        async def _call(contents, **kw):
            if seen is not None:
                seen['contents'] = contents
            return response

        model.generate_content_async = _call
        return model
    return patch('modules.llm.gemini_client.genai.GenerativeModel', side_effect=factory)


def test_a_max_tokens_candidate_reads_as_truncated():
    response = _response(FinishReason.MAX_TOKENS, [glm.Part(text="half")])
    assert finish_reason_of(response) == "MAX_TOKENS"
    assert output_was_truncated(CallFacts(finish_reason=finish_reason_of(response)))


def test_a_stop_candidate_is_not_truncated():
    reason = finish_reason_of(_response(FinishReason.STOP, [glm.Part(text="ok")]))
    assert reason == "STOP"
    assert not output_was_truncated(CallFacts(finish_reason=reason))


def test_a_blocked_candidate_raises_typed_with_the_reason():
    from core.exceptions import LLMRefusalError
    with pytest.raises(LLMRefusalError, match="SAFETY") as caught:
        read_candidate(_response(FinishReason.SAFETY))
    assert caught.value.kind == "filtered" and caught.value.category == "SAFETY"


def test_a_blocked_prompt_raises_typed_with_the_reason():
    feedback = glm.GenerateContentResponse.PromptFeedback(
        block_reason=glm.GenerateContentResponse.PromptFeedback.BlockReason.SAFETY)
    from core.exceptions import LLMRefusalError
    with pytest.raises(LLMRefusalError, match="block_reason SAFETY"):
        read_candidate(SimpleNamespace(prompt_feedback=feedback, candidates=[]))


@pytest.mark.asyncio
async def test_a_blocked_candidate_is_typed_on_both_paths(client):
    from core.exceptions import LLMRefusalError
    with _capture(_response(FinishReason.SAFETY)):
        with pytest.raises(LLMRefusalError):
            await client._generate(messages=[{"role": "user", "content": "x"}], max_tokens=16)
        with pytest.raises(LLMRefusalError):
            await client._generate_with_tools(
                messages=[{"role": "user", "content": "x"}], max_tokens=16,
                tools=[{"name": "t", "parameters": {"type": "object"}}])


@pytest.mark.asyncio
async def test_a_function_response_names_the_function_of_the_prior_row(client):
    seen = {}
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "gemini_call_1", "type": "function",
             "function": {"name": "search", "arguments": {"q": "x"}}}]},
        {"role": "tool", "tool_call_id": "gemini_call_1", "content": "found"},
    ]
    with _capture(_response(parts=[glm.Part(text="ok")]), seen):
        await client._generate_with_tools(
            messages=messages, max_tokens=16, tools=[{"name": "search", "parameters": {"type": "object"}}])
    parts = seen['contents'][-1]['parts']
    assert parts[0].function_response.name == "search"


@pytest.mark.parametrize("explicit,expected", [("kwarg", "kwarg"), (None, "row")])
@pytest.mark.asyncio
async def test_the_system_prompt_rule_is_one_on_both_paths(client, explicit, expected):
    messages = [{"role": "system", "content": "row"}, {"role": "user", "content": "x"}]
    seen = {}
    with _capture(_response(parts=[glm.Part(text="ok")]), seen):
        await client._generate(messages=messages, system=explicit, max_tokens=16)
    assert seen['system_instruction'] == expected
    seen.clear()
    with _capture(_response(parts=[glm.Part(text="ok")]), seen):
        await client._generate_with_tools(messages=messages, system=explicit, max_tokens=16,
                                          tools=[{"name": "t", "parameters": {"type": "object"}}])
    assert seen['system_instruction'] == expected


@pytest.mark.asyncio
async def test_stop_sequences_reach_the_tool_path(client):
    seen = {}
    with _capture(_response(parts=[glm.Part(text="ok")]), seen):
        await client._generate_with_tools(
            messages=[{"role": "user", "content": "x"}], max_tokens=16, stop_sequences=["END"],
            tools=[{"name": "t", "parameters": {"type": "object"}}])
    assert seen['generation_config']['stop_sequences'] == ["END"]


def test_thought_tokens_count_as_output(client):
    client.last_response = _response(prompt_token_count=10, candidates_token_count=5,
                                     thoughts_token_count=7, total_token_count=22)
    usage = client._extract_usage_data()
    assert usage['completion_tokens'] == 12 and usage['total_tokens'] == 22
    assert 'usage_estimated' not in usage


def test_an_estimate_is_marked_and_invents_no_input(client):
    client.last_response = SimpleNamespace(text="x" * 40, usage_metadata=None)
    usage = client._extract_usage_data()
    assert usage['usage_estimated'] is True
    assert usage['completion_tokens'] == 10 and usage['prompt_tokens'] == 0


def test_the_key_stays_out_of_the_process_environment(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
    config = Mock()
    config.get_llm_config.return_value = {'gemini': {'model': 'gemini-2.0-flash', 'api_key': 'test_key_12345'}}
    GeminiClient(config)
    assert 'GEMINI_API_KEY' not in os.environ and 'GOOGLE_API_KEY' not in os.environ


def test_a_list_type_is_nullable():
    schema = json_schema_to_glm(glm, {"type": ["string", "null"], "description": "d"})
    assert schema.type_ == glm.Type.STRING and schema.nullable


def test_any_of_keeps_its_first_concrete_branch():
    schema = json_schema_to_glm(glm, {"type": "object", "properties": {
        "a": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "d"},
        "b": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}]},
    }})
    a, b = schema.properties["a"], schema.properties["b"]
    assert a.type_ == glm.Type.INTEGER and a.nullable and a.description == "d"
    assert b.type_ == glm.Type.ARRAY and b.items.type_ == glm.Type.STRING and not b.nullable


@pytest.mark.parametrize("error,expected", [
    (type("ResourceExhausted", (Exception,), {"code": 429})("quota"), LLMRateLimitError),
    (type("Unauthenticated", (Exception,), {"code": 401})("bad key"), LLMAuthenticationError),
])
@pytest.mark.asyncio
async def test_provider_errors_reach_the_caller_typed(client, error, expected):
    def factory(*args, **kwargs):
        model = Mock()

        async def _raise(*a, **kw):
            raise error

        model.generate_content_async = _raise
        return model

    with patch('modules.llm.gemini_client.genai.GenerativeModel', side_effect=factory):
        with pytest.raises(expected):
            await client.generate_agent_response(
                messages=[{"role": "user", "content": "x"}], max_tokens=16,
                tools=[{"name": "t", "parameters": {"type": "object"}}])
        with pytest.raises(expected):
            await client.generate_response(messages=[{"role": "user", "content": "x"}], max_tokens=16)
