"""A1 — cheap auxiliary compaction model default map.

The compaction call may route to a cheap auxiliary model instead of the
(expensive) main model. Today that only happens when COMPACTION_MODEL is set
explicitly. A1 adds a provider->cheap-model default map consulted ONLY when
COMPACTION_MODEL is unset AND COMPACTION_AUTO_AUX is enabled.

Verified against the generalized `resolve_aux_model("compaction", ...)` resolver
(the former pure `resolve_aux_compaction_model` was folded into it). The compaction
task preserves its legacy env knobs: COMPACTION_MODEL (explicit) + COMPACTION_AUTO_AUX.
"""
import pytest

from agents.task.constants import COMPACTION_AUX_MODEL_MAP, resolve_aux_model


@pytest.fixture(autouse=True)
def _clean_aux_env(monkeypatch):
    # Start every case from a known-empty aux environment.
    for var in ("COMPACTION_MODEL", "COMPACTION_AUTO_AUX", "AUX_AUTO"):
        monkeypatch.delenv(var, raising=False)


def test_unset_returns_none_preserving_current_behavior():
    # No explicit model, auto-aux off -> None (falls back to main model).
    assert resolve_aux_model("compaction", provider="anthropic") is None


def test_explicit_model_wins_even_when_auto_aux_off(monkeypatch):
    monkeypatch.setenv("COMPACTION_MODEL", "claude-opus-4-8")
    assert resolve_aux_model("compaction", provider="anthropic") == "claude-opus-4-8"


def test_explicit_model_wins_over_map_when_auto_aux_on(monkeypatch):
    # Explicit knob always takes precedence over the cheap-map default.
    monkeypatch.setenv("COMPACTION_MODEL", "gpt-5")
    monkeypatch.setenv("COMPACTION_AUTO_AUX", "true")
    assert resolve_aux_model("compaction", provider="openai") == "gpt-5"


def test_auto_aux_maps_known_provider_to_cheap_model(monkeypatch):
    monkeypatch.setenv("COMPACTION_AUTO_AUX", "true")
    assert resolve_aux_model("compaction", provider="anthropic") == COMPACTION_AUX_MODEL_MAP["anthropic"]
    assert resolve_aux_model("compaction", provider="openai") == COMPACTION_AUX_MODEL_MAP["openai"]


def test_auto_aux_openrouter_not_mapped(monkeypatch):
    # OpenRouter has no single obvious cheap default; intentionally NOT auto-mapped —
    # resolve returns None so compaction uses the main model. OpenRouter users can opt
    # in explicitly via COMPACTION_MODEL (same-provider aux is safe — isolated client).
    monkeypatch.setenv("COMPACTION_AUTO_AUX", "true")
    assert resolve_aux_model("compaction", provider="openrouter") is None
    assert "openrouter" not in COMPACTION_AUX_MODEL_MAP


def test_auto_aux_provider_lookup_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("COMPACTION_AUTO_AUX", "true")
    assert resolve_aux_model("compaction", provider="Anthropic") == COMPACTION_AUX_MODEL_MAP["anthropic"]


def test_auto_aux_unknown_provider_returns_none(monkeypatch):
    monkeypatch.setenv("COMPACTION_AUTO_AUX", "true")
    assert resolve_aux_model("compaction", provider="some-unknown-provider") is None


def test_auto_aux_with_no_provider_returns_none(monkeypatch):
    monkeypatch.setenv("COMPACTION_AUTO_AUX", "true")
    assert resolve_aux_model("compaction", provider=None) is None
