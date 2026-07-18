"""Regression (P4 finalization): prompt_cache_enabled used an inline falsey set that
missed "none" — LLM_PROMPT_CACHE=none read as DISABLED everywhere else (core.env SSOT)
but was IGNORED here. Now uses core.env.bool_env.
"""
import pytest

from modules.llm.cache_hints import prompt_cache_enabled


@pytest.mark.parametrize("val,expected", [
    ("none", False), ("off", False), ("false", False), ("0", False),
    ("1", True), ("true", True), ("on", True),
])
def test_prompt_cache_honors_ssot_falsey_set(monkeypatch, val, expected):
    monkeypatch.setenv("LLM_PROMPT_CACHE", val)
    monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE", raising=False)
    assert prompt_cache_enabled() is expected


def test_prompt_cache_default_on(monkeypatch):
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("ANTHROPIC_PROMPT_CACHE", raising=False)
    assert prompt_cache_enabled() is True
