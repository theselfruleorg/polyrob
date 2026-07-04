"""P0.7: OpenRouterClient must fall back to the registry default, not a stale
hardcoded literal, when no explicit model is configured.

Regression context: ``openrouter_client.py`` constructed its default model with
``openrouter_config.get('model', 'x-ai/grok-4.3')`` — a literal that had drifted
from ``DEFAULT_MODELS['openrouter']`` (now ``z-ai/glm-5.2``) in the registry.
``BotConfig.get_llm_config()['openrouter']`` never actually carries a ``'model'``
key, so that hardcoded fallback was always the value in play for any caller that
builds an ``OpenRouterClient`` without immediately overwriting ``model_type``
(e.g. ``scripts/test_openrouter.py``). The fix routes the fallback through
``get_default_model('openrouter')`` — the single source of truth also used by
``LLMManager.FALLBACK_HIERARCHY`` and every other provider client.
"""

import pytest

from core.config import BotConfig
from modules.llm.llm_client_registry import get_default_model, DEFAULT_MODELS
from modules.llm.openrouter_client import OpenRouterClient


def _config_with_openrouter_key(monkeypatch) -> BotConfig:
    """A BotConfig whose ``get_llm_config()['openrouter']`` has no 'model' key
    (matches production: BotConfig never populates one), only an api_key so
    ``OpenRouterClient.__init__`` doesn't warn about a missing key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)
    return BotConfig()


def test_openrouter_client_uses_registry_default_when_model_unset(monkeypatch):
    """No explicit 'model' in config, no env override -> registry default, not
    the stale grok-4.3 literal."""
    monkeypatch.delenv("POLYROB_OPENROUTER_MODEL", raising=False)
    config = _config_with_openrouter_key(monkeypatch)

    # Sanity-check the premise: BotConfig really doesn't surface a 'model' key,
    # so the client is exercising the fallback branch, not an explicit config.
    assert "model" not in config.get_llm_config()["openrouter"]

    client = OpenRouterClient(config)

    assert client.model_type == get_default_model("openrouter")
    assert client.model_type == DEFAULT_MODELS["openrouter"]  # == 'z-ai/glm-5.2'
    assert client.model_type != "x-ai/grok-4.3"


def test_openrouter_client_honors_per_provider_env_override(monkeypatch):
    """get_default_model()'s POLYROB_OPENROUTER_MODEL override must flow through
    the constructor too (proves the fallback is live-wired to the SSOT resolver,
    not just coincidentally equal to it at import time)."""
    monkeypatch.setenv("POLYROB_OPENROUTER_MODEL", "some/other-model")
    config = _config_with_openrouter_key(monkeypatch)

    client = OpenRouterClient(config)

    assert client.model_type == "some/other-model"


def test_openrouter_client_explicit_model_still_wins(monkeypatch):
    """An explicit 'model' key in the llm config (e.g. from create_llm_client's
    caller-supplied config) must still take precedence over the registry
    default — the fallback only fires when 'model' is absent/falsy."""
    monkeypatch.delenv("POLYROB_OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-" + "x" * 24)

    class _FakeConfigWithModel:
        def get_llm_config(self):
            return {"openrouter": {"api_key": "sk-or-" + "x" * 24, "model": "explicit/model"}}

        def get(self, key, default=None):
            # LLMClient.__init__ reads config.get('max_retries'/'retry_delay', ...)
            # directly (BotConfig.get() is a generic getattr-style accessor).
            return default

    client = OpenRouterClient(_FakeConfigWithModel())

    assert client.model_type == "explicit/model"
