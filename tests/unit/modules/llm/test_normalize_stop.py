"""One stop enum: any provider's stop reason reads as one of six values."""
from types import SimpleNamespace

import pytest

from core.exceptions import LLMResponseError
from core.stop_reason import normalize_stop
from modules.llm.call_result import CallFacts, output_was_truncated
from modules.llm.llm_client import LLMClient


@pytest.mark.parametrize("reason,stop", [
    ("stop", "complete"), ("end_turn", "complete"), ("STOP", "complete"), ("completed", "complete"),
    ("length", "length"), ("max_tokens", "length"), ("MAX_TOKENS", "length"),
    ("max_output_tokens", "length"), ("model_context_window_exceeded", "length"),
    ("content_filter", "filtered"), ("SAFETY", "filtered"), ("RECITATION", "filtered"),
    ("refusal", "refused"),
    ("error", "error"), ("MALFORMED_FUNCTION_CALL", "error"),
    ("tool_calls", "tool_calls"), ("tool_use", "tool_calls"),
])
def test_each_provider_reason_has_one_value(reason, stop):
    assert normalize_stop(reason) == stop


@pytest.mark.parametrize("reason", [None, "", "a_new_reason", 7])
def test_an_unknown_reason_is_unknown_not_complete(reason):
    assert normalize_stop(reason) is None


@pytest.mark.parametrize("reason", [
    "length", "max_tokens", "MAX_TOKENS", "model_length", "max_output_tokens",
    "model_context_window_exceeded", "Length",
])
def test_each_length_reason_reads_as_truncated(reason):
    assert output_was_truncated(CallFacts(finish_reason=reason))


@pytest.mark.parametrize("reason", ["stop", "tool_calls", "SAFETY", None, "a_new_reason"])
def test_every_other_reason_does_not_read_as_truncated(reason):
    assert not output_was_truncated(CallFacts(finish_reason=reason))


def test_call_facts_carry_the_normalized_stop():
    assert CallFacts(finish_reason="MAX_TOKENS").stop == "length"
    assert CallFacts().stop is None


def test_the_client_refuses_a_filtered_choice_of_any_provider():
    client = SimpleNamespace(model_type="m", provider_label="p")
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="SAFETY", message=None)])
    from core.exceptions import LLMRefusalError
    with pytest.raises(LLMRefusalError, match="content filter") as caught:
        LLMClient._first_choice(client, response)
    assert caught.value.kind == "filtered" and caught.value.category == "SAFETY"
