"""057 WS-B: the LLM timeout scales on OUTPUT, not on input.

Measured on prod over 24 h: <=1k output -> 13 s, 5k -> 101 s, 16k -> 272 s, while
uncached INPUT was flat at 41-63 s across every bucket. The legacy ladder handed
a 600 s budget to a small-output call in a big context and 180 s to the 16k-token
file write that actually needed 270 s.
"""
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="t", action_descriptions="a",
                          system_prompt_class=SystemPrompt,
                          max_input_tokens=200000, session_id="tbo-1")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_BY_OUTPUT", raising=False)
    monkeypatch.delenv("AUTOV2_LLM_TIMEOUT_OVERRIDE", raising=False)
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)


def test_legacy_ladder_is_the_default():
    mm = _mm()
    mm.history.total_tokens = 60000
    assert mm.calculate_llm_timeout(tool_count=180) == pytest.approx(390.0)


def test_note_call_usage_keeps_the_last_two_outputs_and_uncached_input():
    mm = _mm()
    mm.note_call_usage(output_tokens=100, input_tokens=50000, cached_tokens=40000)
    mm.note_call_usage(output_tokens=9000, input_tokens=60000, cached_tokens=48000)
    mm.note_call_usage(output_tokens=200, input_tokens=61000, cached_tokens=48000)
    assert mm._recent_output_tokens == [9000, 200]
    assert mm._last_uncached_input == 13000


def test_a_big_context_with_small_output_is_not_given_ten_minutes(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_BY_OUTPUT", "true")
    mm = _mm()
    mm.history.total_tokens = 600000          # would have been 600 s
    mm.note_call_usage(output_tokens=300, input_tokens=40000, cached_tokens=38000)
    mm.note_call_usage(output_tokens=250, input_tokens=41000, cached_tokens=39000)
    # 90 + 300/40 + 2000/10000 = 97.7 -> floored at 120
    assert mm.calculate_llm_timeout(tool_count=180) == pytest.approx(120.0)


def test_a_big_write_widens_the_next_budget(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_BY_OUTPUT", "true")
    mm = _mm()
    mm.note_call_usage(output_tokens=200, input_tokens=20000, cached_tokens=20000)
    mm.note_call_usage(output_tokens=16000, input_tokens=20000, cached_tokens=20000)
    # 90 + 16000/40 + 0 = 490
    assert mm.calculate_llm_timeout() == pytest.approx(490.0)


def test_the_expectation_is_capped_at_this_session_output_ceiling(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_BY_OUTPUT", "true")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "4096")
    mm = _mm()
    mm.note_call_usage(output_tokens=60000, input_tokens=10000, cached_tokens=10000)
    # the provider may not emit more than 4096, so the budget must not assume it
    assert mm.calculate_llm_timeout() == pytest.approx(max(120.0, 90.0 + 4096 / 40.0))


def test_uncached_input_adds_a_second_per_10k(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_BY_OUTPUT", "true")
    mm = _mm()
    mm.note_call_usage(output_tokens=8000, input_tokens=150000, cached_tokens=50000)
    # 90 + 8000/40 + 100000/10000 = 300
    assert mm.calculate_llm_timeout() == pytest.approx(300.0)


def test_no_history_yet_uses_a_stated_default(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_BY_OUTPUT", "true")
    mm = _mm()
    assert mm.calculate_llm_timeout() == pytest.approx(120.0)  # 90 + 1024/40 = 115.6 -> floor


def test_the_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_BY_OUTPUT", "true")
    monkeypatch.setenv("AUTOV2_LLM_TIMEOUT_OVERRIDE", "42")
    assert _mm().calculate_llm_timeout() == pytest.approx(42.0)


def test_both_billing_sites_feed_the_timeout():
    """Regression (prod 2026-09-20 04:50Z): the NATIVE-tools billing site (purpose
    "next_action", the path prod runs) billed but never called note_call_usage, so
    the output-scaled timeout read the 1,024-token default on every call. Pin both
    sites — a third billing path added without the feed would silently regress."""
    import inspect
    from agents.task.agent.core import next_action_internal as m
    src = inspect.getsource(m)
    assert src.count("self.message_manager.note_call_usage(") == 2
    assert src.count("self.usage_tracker.record_llm_usage(") == 2
