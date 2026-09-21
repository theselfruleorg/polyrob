"""057 WS-B: the per-session output budget and the truncation signal."""
import types

import pytest

from agents.task.session_class import apply_session_output_budget
from modules.llm.output_budget import (
    apply_output_budget, finish_reason_of, last_output_tokens,
    output_token_cap, output_was_truncated,
)
from agents.task.goals.autonomy_marker import mark_autonomous


class _Client:
    def __init__(self, response=None):
        self.last_response = response


class _Llm:
    def __init__(self, client):
        self._client = client


def _openai_response(reason, completion_tokens=None):
    usage = types.SimpleNamespace(completion_tokens=completion_tokens)
    choice = types.SimpleNamespace(finish_reason=reason)
    return types.SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)


def test_cap_is_the_existing_ceiling_by_default():
    assert output_token_cap(_Client(), 16384) == 16384


def test_env_ceiling_still_wins(monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    assert output_token_cap(_Client(), 16384) == 4096


def test_budget_is_off_without_the_flag():
    mark_autonomous("ob-goal-1", "g1")
    llm = _Llm(_Client())
    assert apply_session_output_budget(llm, "ob-goal-1") == 0
    assert output_token_cap(llm._client, 16384) == 16384


def test_autonomous_session_is_narrowed(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    mark_autonomous("ob-goal-2", "g2")
    llm = _Llm(_Client())
    assert apply_session_output_budget(llm, "ob-goal-2") == 8192
    assert output_token_cap(llm._client, 16384) == 8192


def test_an_owner_chat_is_not_narrowed(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    llm = _Llm(_Client())
    assert apply_session_output_budget(llm, "ob-chat-1") == 0
    assert output_token_cap(llm._client, 16384) == 16384


def test_the_budget_never_raises_a_lower_ceiling(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "2048")
    mark_autonomous("ob-goal-3", "g3")
    llm = _Llm(_Client())
    apply_session_output_budget(llm, "ob-goal-3")
    assert output_token_cap(llm._client, 16384) == 2048


def test_finish_reason_openai_shape():
    c = _Client(_openai_response("length", 16384))
    assert finish_reason_of(c) == "length"
    assert output_was_truncated(_Llm(c)) is True
    assert last_output_tokens(_Llm(c)) == 16384


def test_finish_reason_dict_shape():
    c = _Client({"choices": [{"finish_reason": "length"}],
                 "usage": {"completion_tokens": 4096}})
    assert finish_reason_of(c) == "length"
    assert output_was_truncated(_Llm(c)) is True
    assert last_output_tokens(_Llm(c)) == 4096


def test_finish_reason_anthropic_shape():
    c = _Client(types.SimpleNamespace(stop_reason="max_tokens"))
    assert finish_reason_of(c) == "max_tokens"
    assert output_was_truncated(_Llm(c)) is True


def test_a_clean_stop_is_not_truncation():
    assert output_was_truncated(_Llm(_Client(_openai_response("stop", 120)))) is False
    assert output_was_truncated(_Llm(_Client(_openai_response("tool_calls")))) is False


def test_unreadable_response_is_unknown_never_a_confident_no():
    assert finish_reason_of(_Client(None)) is None
    assert finish_reason_of(_Client(types.SimpleNamespace())) is None
    # unknown must not be reported as truncated either
    assert output_was_truncated(_Llm(_Client(None))) is False


def test_modules_tier_stamping_is_session_agnostic():
    """The layering half: modules/llm stamps, agents decides who gets one."""
    llm = _Llm(_Client())
    assert apply_output_budget(llm, 4096) == 4096
    assert output_token_cap(llm._client, 16384) == 4096
    assert apply_output_budget(None, 4096) == 0
    assert apply_output_budget(llm, 0) == 0
