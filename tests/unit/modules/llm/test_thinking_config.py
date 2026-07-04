"""UP-07 Steps 7.4-7.5 — registry thinking config + per-provider consumption (gated)."""
import pytest

from modules.llm.model_registry import (
    get_thinking_config, thinking_config_enabled, get_model_config)


# --- registry -----------------------------------------------------------------

def test_anthropic_budget_from_registry():
    cfg = get_thinking_config("claude-sonnet-4-5")
    assert cfg.get("budget_tokens") == 64000
    assert get_thinking_config("claude-opus-4-1").get("budget_tokens") == 32768


def test_deepseek_reasoner_budget():
    assert get_thinking_config("deepseek-reasoner").get("budget_tokens") == 32000


def test_non_thinking_or_unknown_model_empty():
    assert get_thinking_config("definitely-not-a-model") == {}


def test_extended_thinking_models_dict_removed():
    from modules.llm.anthropic_client import AnthropicClient
    assert not hasattr(AnthropicClient, "EXTENDED_THINKING_MODELS")


def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("THINKING_CONFIG_ENABLED", raising=False)
    assert thinking_config_enabled() is False
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    assert thinking_config_enabled() is True


# --- DeepSeek consumption ------------------------------------------------------

def _deepseek():
    import logging
    from modules.llm.deepseek_client import DeepSeekClient
    c = object.__new__(DeepSeekClient)
    c.logger = logging.getLogger("deepseek-thinking-test")
    c.model_type = "deepseek-reasoner"
    c.temperature = 0.7
    return c


def test_deepseek_cot_from_registry_when_gated_on(monkeypatch):
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    c = _deepseek()
    body = c._build_request_body([{"role": "user", "content": "hi"}], max_tokens=100)
    assert body["max_cot_tokens"] == 32000


def test_deepseek_cot_falls_back_to_const_when_gated_off(monkeypatch):
    monkeypatch.delenv("THINKING_CONFIG_ENABLED", raising=False)
    c = _deepseek()
    body = c._build_request_body([{"role": "user", "content": "hi"}], max_tokens=100)
    assert body["max_cot_tokens"] == c.DEFAULT_MAX_COT_TOKENS
