"""Tests for Anthropic prompt-cache breakpoint injection (flow-efficiency D4-a).

The system prompt (+ tool defs) is stable across steps within a session, so a
cache_control breakpoint on the last system block lets Anthropic serve the whole
tools+system prefix from cache (~10x cheaper repeated input).
"""

import pytest

from modules.llm.anthropic_client import _build_cached_system_param


def test_string_system_becomes_cached_block():
    out = _build_cached_system_param("You are a helpful agent.")
    assert out == [
        {"type": "text", "text": "You are a helpful agent.",
         "cache_control": {"type": "ephemeral"}}
    ]


def test_list_system_caches_only_last_block():
    out = _build_cached_system_param([
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two"},
    ])
    assert "cache_control" not in out[0]
    assert out[1]["cache_control"] == {"type": "ephemeral"}
    # original text preserved
    assert out[0]["text"] == "part one" and out[1]["text"] == "part two"


def test_falsy_system_returns_none():
    assert _build_cached_system_param("") is None
    assert _build_cached_system_param(None) is None


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE", "0")
    out = _build_cached_system_param("stable prompt")
    assert out == [{"type": "text", "text": "stable prompt"}]
    assert "cache_control" not in out[0]


def test_does_not_mutate_input_list():
    src = [{"type": "text", "text": "x"}]
    _build_cached_system_param(src)
    assert "cache_control" not in src[0]  # input untouched
