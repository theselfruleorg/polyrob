"""D-2 / D-3 / D-7 (review 2026-10-14): the usage record carries the upstream
that served the call, the reasoning tokens, and the agent step's context slots.

All three ride the ONE stamp seam (`prefix_stamp.read_stamps`), which both the
main step billing site and `meter_aux_llm` already read.
"""
import types

from openai.types.chat import ChatCompletion

from modules.llm.call_scope import begin_call, set_step_context
from modules.llm.call_result import call_facts, read_stamps, response_stamps
from modules.llm.prefix_stamp import context_stamps, stamp_request


def _openrouter_completion(provider="DeepInfra", reasoning=37):
    return ChatCompletion.model_validate({
        "id": "gen-1", "object": "chat.completion", "created": 1, "model": "deepseek/x",
        "provider": provider,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}},
    })


def test_sdk_response_upstream_and_reasoning_tokens():
    assert response_stamps(_openrouter_completion()) == {"upstream": "DeepInfra", "reasoning_tokens": 37}


def test_zero_reasoning_and_missing_provider_are_absent():
    resp = _openrouter_completion(provider=None, reasoning=0)
    assert response_stamps(resp) == {}


def test_dict_response_shape_and_responses_api_shape():
    assert response_stamps({"provider": "Novita", "usage": {
        "completion_tokens_details": {"reasoning_tokens": 5}}}) == {"upstream": "Novita", "reasoning_tokens": 5}
    resp = types.SimpleNamespace(usage=types.SimpleNamespace(
        output_tokens_details=types.SimpleNamespace(reasoning_tokens=9)))
    assert response_stamps(resp) == {"reasoning_tokens": 9}


def test_garbage_never_raises():
    assert response_stamps(None) == {}
    assert response_stamps(object()) == {}
    assert response_stamps({"provider": 3, "usage": "x"}) == {}


def test_read_stamps_reads_the_facts_on_the_message():
    request = begin_call()
    stamp_request({"messages": [{"role": "system", "content": "s"}]})
    message = types.SimpleNamespace(call_facts=call_facts(_openrouter_completion(), None, request))
    out = read_stamps(message)
    assert out["upstream"] == "DeepInfra" and out["reasoning_tokens"] == 37
    assert "prefix_sha" in out and out["finish_reason"] == "stop"


_USAGE = {"total_tokens": 149000, "limit": 200000, "pct": 74.53, "source": "provider_usage",
          "slots": {"foundation": 30000, "hmem": 1200, "conversation": 50000,
                    "ephemeral": 800, "tool_schemas": 55000}}


def test_context_stamps_flatten_the_one_gauge():
    assert context_stamps(_USAGE) == {
        "ctx_foundation": 30000, "ctx_hmem": 1200, "ctx_conversation": 50000,
        "ctx_ephemeral": 800, "ctx_schemas": 55000, "ctx_limit": 200000,
        "ctx_pct": 74.5, "ctx_source": "provider_usage"}
    assert context_stamps(None) == {} and context_stamps({"slots": "x"}) == {}


def test_step_context_rides_the_call_and_an_aux_record_omits_it():
    set_step_context(context_stamps(_USAGE))
    try:
        message = types.SimpleNamespace(call_facts=call_facts(None, None, begin_call()))
    finally:
        set_step_context(None)
    assert read_stamps(message, include_context=False).get("ctx_pct") is None
    assert read_stamps(message)["ctx_schemas"] == 55000 and read_stamps(message)["ctx_pct"] == 74.5
    later = types.SimpleNamespace(call_facts=call_facts(None, None, begin_call()))
    assert "ctx_schemas" not in read_stamps(later), "a later unstamped call must not inherit it"
