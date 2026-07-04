"""Per-provider env override for the default model (live-test model-swap knob).

The headless `polyrob telegram` path has no model selector — it uses
DEFAULT_MODELS[provider]. POLYROB_<PROVIDER>_MODEL lets a deploy pin/swap the
model with just an env change + restart (e.g. test grok instead of glm).
"""

import pytest

from modules.llm.llm_client_registry import get_default_model, DEFAULT_MODELS


def test_no_override_returns_policy_default(monkeypatch):
    monkeypatch.delenv("POLYROB_OPENROUTER_MODEL", raising=False)
    assert get_default_model("openrouter") == DEFAULT_MODELS["openrouter"]


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("POLYROB_OPENROUTER_MODEL", "x-ai/grok-4.3")
    assert get_default_model("openrouter") == "x-ai/grok-4.3"


def test_override_is_per_provider(monkeypatch):
    monkeypatch.setenv("POLYROB_OPENROUTER_MODEL", "x-ai/grok-4.3")
    monkeypatch.delenv("POLYROB_ANTHROPIC_MODEL", raising=False)
    # openrouter overridden, anthropic untouched
    assert get_default_model("openrouter") == "x-ai/grok-4.3"
    assert get_default_model("anthropic") == DEFAULT_MODELS["anthropic"]


def test_blank_override_ignored(monkeypatch):
    monkeypatch.setenv("POLYROB_OPENROUTER_MODEL", "   ")
    assert get_default_model("openrouter") == DEFAULT_MODELS["openrouter"]
