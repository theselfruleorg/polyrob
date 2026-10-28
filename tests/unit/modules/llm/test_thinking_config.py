"""UP-07 Steps 7.4-7.5 — registry thinking config + per-provider consumption (gated)."""
import pytest

from modules.llm.model_registry import (
    get_thinking_config, thinking_config_enabled, get_model_config)


# --- registry -----------------------------------------------------------------

def test_anthropic_budget_from_registry():
    cfg = get_thinking_config("claude-sonnet-4-5")
    assert cfg.get("budget_tokens") == 64000
    assert get_thinking_config("claude-opus-4-1").get("budget_tokens") == 32768


def test_deepseek_thinks_by_server_default():
    # DeepSeek's thinking is ON server-side; the registry sends no budget/effort.
    assert get_thinking_config("deepseek-flash") == {}


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
    c.model_type = "deepseek-flash"
    c.temperature = 0.7
    return c


def test_deepseek_effort_from_registry_when_gated_on(monkeypatch):
    import modules.llm.model_registry as mr
    monkeypatch.setenv("THINKING_CONFIG_ENABLED", "true")
    monkeypatch.setattr(mr, "get_thinking_config", lambda m: {"reasoning_effort": "max"})
    body = _deepseek().request_extras({"model": "deepseek-flash"}, 100)
    assert body["reasoning_effort"] == "max"


def test_deepseek_sends_no_openrouter_extras_when_gated_off(monkeypatch):
    monkeypatch.delenv("THINKING_CONFIG_ENABLED", raising=False)
    body = _deepseek().request_extras({"model": "deepseek-flash"}, 100)
    assert "reasoning_effort" not in body
    assert not (body.get("extra_body") or {}).keys() & {"usage", "reasoning", "provider"}
