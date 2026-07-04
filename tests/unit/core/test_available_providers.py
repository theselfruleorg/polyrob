"""AgentConfig.available_providers() — config-object 'which providers have keys'
oracle (Seam 1 wrapper, Phase 1a). Mirrors profiles.providers_with_keys but reads
the config's own *_api_key fields, in canonical PROFILES order.

Provider key fields use env aliases (OPENROUTER_API_KEY, ...), so we populate via
the environment (the real mechanism) rather than direct kwargs.
"""
import pytest

from core.config import AgentConfig

_PROVIDER_ENV = [
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
]


@pytest.fixture
def no_provider_keys(monkeypatch):
    for k in _PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_available_providers_openrouter_only(no_provider_keys):
    no_provider_keys.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 30)
    assert AgentConfig().available_providers() == ["openrouter"]


def test_available_providers_none_when_empty(no_provider_keys):
    assert AgentConfig().available_providers() == []


def test_available_providers_canonical_order(no_provider_keys):
    no_provider_keys.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 30)
    no_provider_keys.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 30)
    # PROFILES order (2026-06-24): openrouter is the preferred default, before anthropic
    assert AgentConfig().available_providers() == ["openrouter", "anthropic"]
