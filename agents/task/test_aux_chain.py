"""Per-feature aux-model slots + per-task fallback chains (B5, Hermes parity).

resolve_aux_chain() wraps resolve_aux_model() to return an ORDERED list of
provider/model candidates for an aux task (compaction/judge/reflection),
instead of a single model string. Empty list => caller falls back to the
main model (unchanged runtime contract).
"""
import pytest

from agents.task.constants import resolve_aux_chain

# Every env var resolve_aux_chain / resolve_aux_model can read, across all 3 slots.
_ALL_AUX_ENV_KEYS = (
    "AUX_AUTO",
    "COMPACTION_AUTO_AUX",
    "COMPACTION_MODEL",
    "COMPACTION_PROVIDER",
    "AUX_MODEL_JUDGE",
    "AUX_PROVIDER",
    "AUX_MODEL_COMPACTION",
    "AUX_PROVIDER_COMPACTION",
    "AUX_FALLBACK_COMPACTION",
    "AUX_MODEL_JUDGE",
    "AUX_PROVIDER_JUDGE",
    "AUX_FALLBACK_JUDGE",
    "AUX_MODEL_REFLECTION",
    "AUX_PROVIDER_REFLECTION",
    "AUX_FALLBACK_REFLECTION",
)


@pytest.fixture(autouse=True)
def _clean_aux_env(monkeypatch):
    """Isolate every test from whatever the dev/CI environment happens to set."""
    for key in _ALL_AUX_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_primary_plus_fallbacks(monkeypatch):
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_PROVIDER_JUDGE", "anthropic")
    monkeypatch.setenv("AUX_FALLBACK_JUDGE", "openai/gpt-5-mini,gemini/gemini-flash")
    chain = resolve_aux_chain("judge", "openrouter")
    assert chain[0] == {"model": "claude-haiku-4-5", "provider": "anthropic"}
    assert chain[1] == {"model": "gpt-5-mini", "provider": "openai"}
    assert chain[2] == {"model": "gemini-flash", "provider": "gemini"}


def test_legacy_compaction_env_still_works(monkeypatch):
    monkeypatch.setenv("COMPACTION_MODEL", "gpt-5-mini")
    assert resolve_aux_chain("compaction", "openai")[0]["model"] == "gpt-5-mini"


def test_empty_when_unset():
    assert resolve_aux_chain("judge", "openrouter") == []


def test_new_compaction_env_takes_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("COMPACTION_MODEL", "gpt-5-mini")
    monkeypatch.setenv("AUX_MODEL_COMPACTION", "claude-haiku-4-5")
    assert resolve_aux_chain("compaction", "openai")[0]["model"] == "claude-haiku-4-5"


def test_bare_model_fallback_keeps_default_provider(monkeypatch):
    # A fallback token with no "/" is a bare model name; it should NOT inherit the
    # primary candidate's provider (default_provider=None per the sketch) so the
    # factory auto-detects it from the model registry.
    monkeypatch.setenv("AUX_MODEL_JUDGE", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_FALLBACK_JUDGE", "gpt-5-mini")
    chain = resolve_aux_chain("judge", "openai")
    assert chain[1] == {"model": "gpt-5-mini", "provider": None}


def test_reflection_inherits_compaction_model_and_provider_when_unset(monkeypatch):
    monkeypatch.setenv("AUX_MODEL_COMPACTION", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_PROVIDER_COMPACTION", "anthropic")
    chain = resolve_aux_chain("reflection", "openai")
    assert chain[0] == {"model": "claude-haiku-4-5", "provider": "anthropic"}


def test_reflection_inherits_legacy_compaction_provider_when_model_inherited(monkeypatch):
    # Legacy knobs only (no new AUX_MODEL_COMPACTION/AUX_PROVIDER_COMPACTION set):
    # reflection must still inherit COMPACTION_MODEL + COMPACTION_PROVIDER together,
    # not fall through to the generic AUX_PROVIDER.
    monkeypatch.setenv("COMPACTION_MODEL", "gpt-5-mini")
    monkeypatch.setenv("COMPACTION_PROVIDER", "openai")
    monkeypatch.setenv("AUX_PROVIDER", "gemini")  # decoy: must NOT win
    chain = resolve_aux_chain("reflection", "anthropic")
    assert chain[0] == {"model": "gpt-5-mini", "provider": "openai"}


def test_reflection_uses_own_env_when_set(monkeypatch):
    # Reflection has its OWN model+provider+fallback configured: compaction's config
    # (even if also set) must NOT be consulted at all.
    monkeypatch.setenv("AUX_MODEL_COMPACTION", "gpt-5-mini")
    monkeypatch.setenv("AUX_PROVIDER_COMPACTION", "openai")
    monkeypatch.setenv("AUX_MODEL_REFLECTION", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_PROVIDER_REFLECTION", "anthropic")
    monkeypatch.setenv("AUX_FALLBACK_REFLECTION", "gemini/gemini-flash")
    chain = resolve_aux_chain("reflection", "openai")
    assert chain[0] == {"model": "claude-haiku-4-5", "provider": "anthropic"}
    assert chain[1] == {"model": "gemini-flash", "provider": "gemini"}


def test_reflection_own_model_set_own_provider_unset_does_not_leak_compaction_provider(monkeypatch):
    # Reflection sets its OWN model but not its OWN provider, while compaction has a
    # provider configured. Since the model did NOT come from compaction-inheritance,
    # compaction's provider must NOT apply — falls through to the generic AUX_PROVIDER
    # (or None).
    monkeypatch.setenv("AUX_MODEL_COMPACTION", "gpt-5-mini")
    monkeypatch.setenv("AUX_PROVIDER_COMPACTION", "openai")
    monkeypatch.setenv("AUX_MODEL_REFLECTION", "claude-haiku-4-5")
    chain = resolve_aux_chain("reflection", "anthropic")
    assert chain[0] == {"model": "claude-haiku-4-5", "provider": None}


def test_reflection_fallback_inherits_compaction_fallback_when_unset(monkeypatch):
    monkeypatch.setenv("AUX_MODEL_COMPACTION", "claude-haiku-4-5")
    monkeypatch.setenv("AUX_FALLBACK_COMPACTION", "openai/gpt-5-mini")
    chain = resolve_aux_chain("reflection", "anthropic")
    assert len(chain) == 2
    assert chain[1] == {"model": "gpt-5-mini", "provider": "openai"}


def test_reflection_aux_auto_pairs_legacy_compaction_provider(monkeypatch):
    # Regression (B5 review): with all model envs unset, global AUX_AUTO=true and
    # legacy COMPACTION_PROVIDER set, reflection must resolve via the COMPACTION
    # inheritance branch (model from the cheap map + COMPACTION_PROVIDER paired) —
    # matching pre-B5 behavior where reflection reused _provision_compaction_llm().
    # The bug: resolve_aux_model("reflection") hit the global-AUX_AUTO early-exit
    # first, so inherited_from_compaction stayed False and the provider was dropped.
    monkeypatch.setenv("AUX_AUTO", "true")
    monkeypatch.setenv("COMPACTION_PROVIDER", "anthropic")
    chain = resolve_aux_chain("reflection", "openai")
    assert chain[0] == {"model": "gpt-5-mini", "provider": "anthropic"}


def test_unknown_slot_returns_empty_chain(monkeypatch):
    # The slot set is fixed at the 3 real aux call sites (_AUX_SLOTS); an unknown
    # task never gets a chain — even under AUX_AUTO (dead-config trap guard).
    monkeypatch.setenv("AUX_AUTO", "true")
    assert resolve_aux_chain("planner", "anthropic") == []
    assert resolve_aux_chain("vision", "anthropic") == []


def test_compaction_and_judge_do_not_cross_inherit():
    # Sanity: compaction/judge never inherit each other (only reflection inherits
    # from compaction).
    assert resolve_aux_chain("compaction", "openai") == []
    assert resolve_aux_chain("judge", "openai") == []
