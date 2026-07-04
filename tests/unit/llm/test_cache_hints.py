"""P1-3: provider-agnostic prompt-cache policy seam."""
import pytest

from modules.llm.cache_hints import (
    prompt_cache_enabled,
    openrouter_cache_strategy,
    apply_openrouter_cache_control,
)


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE", raising=False)
    assert prompt_cache_enabled() is True


@pytest.mark.parametrize("var", ["LLM_PROMPT_CACHE", "ANTHROPIC_PROMPT_CACHE"])
def test_kill_switch_either_var(monkeypatch, var):
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE", raising=False)
    monkeypatch.setenv(var, "0")
    assert prompt_cache_enabled() is False


@pytest.mark.parametrize("model,expected", [
    ("anthropic/claude-opus-4", "breakpoints"),
    ("google/gemini-3.1-pro", "breakpoints"),
    ("openai/gpt-5.5", "automatic"),
    ("deepseek/deepseek-chat", "automatic"),
    ("x-ai/grok-4.1-fast", "automatic"),
    ("some/unknown-model", "none"),
    ("", "none"),
    (None, "none"),
])
def test_openrouter_strategy(model, expected):
    assert openrouter_cache_strategy(model) == expected


def test_apply_noop_when_passthrough_disabled(monkeypatch):
    monkeypatch.delenv("OPENROUTER_PROMPT_CACHE", raising=False)
    msgs = [{"role": "system", "content": "big stable prefix"}]
    out = apply_openrouter_cache_control(msgs, "anthropic/claude-opus-4")
    assert out[0]["content"] == "big stable prefix"  # unchanged (string)


def test_apply_adds_breakpoint_when_enabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROMPT_CACHE", "true")
    monkeypatch.setenv("LLM_PROMPT_CACHE", "1")
    msgs = [{"role": "system", "content": "big stable prefix"},
            {"role": "user", "content": "hi"}]
    out = apply_openrouter_cache_control(msgs, "anthropic/claude-opus-4")
    sys_content = out[0]["content"]
    assert isinstance(sys_content, list)
    assert sys_content[-1]["cache_control"] == {"type": "ephemeral"}
    assert out[1]["content"] == "hi"  # non-system untouched


def test_apply_noop_for_automatic_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROMPT_CACHE", "true")
    msgs = [{"role": "system", "content": "prefix"}]
    out = apply_openrouter_cache_control(msgs, "openai/gpt-5.5")
    assert out[0]["content"] == "prefix"  # automatic caching -> no request change


def test_apply_noop_when_globally_disabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROMPT_CACHE", "true")
    monkeypatch.setenv("LLM_PROMPT_CACHE", "0")
    msgs = [{"role": "system", "content": "prefix"}]
    out = apply_openrouter_cache_control(msgs, "anthropic/claude-opus-4")
    assert out[0]["content"] == "prefix"
