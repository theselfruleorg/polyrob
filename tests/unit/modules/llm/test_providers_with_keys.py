"""Tests for the single 'which providers have keys' oracle (Seam 1).

providers_with_keys(env) is the one place that answers "which LLM providers have
an API key present", driven off PROFILES[*].env_key. It replaces the 5 divergent
key-list implementations across the CLI.
"""
from modules.llm.profiles import (
    PROFILES,
    initializable_providers_with_keys,
    no_key_message,
    providers_with_keys,
)


def test_openrouter_only_key_detected():
    # The reported bug: an OpenRouter-only setup must be detected (the old 3-provider
    # has_key check ignored OpenRouter/DeepSeek/NVIDIA).
    assert providers_with_keys({"OPENROUTER_API_KEY": "sk-x"}) == ["openrouter"]


def test_six_providers_covered():
    env = {
        "ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b", "GEMINI_API_KEY": "c",
        "OPENROUTER_API_KEY": "d", "NVIDIA_API_KEY": "e", "DEEPSEEK_API_KEY": "f",
    }
    assert set(providers_with_keys(env)) == {
        "anthropic", "openai", "gemini", "openrouter", "nvidia", "deepseek",
    }


def test_profiles_order_is_canonical():
    # The returned order IS the PROFILES insertion order — the canonical preference
    # order for 'first provider with a key'. OpenRouter is FIRST (2026-06-24): it is
    # the preferred default client whenever its key is present.
    env = {
        "DEEPSEEK_API_KEY": "f", "OPENROUTER_API_KEY": "d", "ANTHROPIC_API_KEY": "a",
    }
    assert providers_with_keys(env) == ["openrouter", "anthropic", "deepseek"]


def test_openrouter_is_preferred_default_when_key_present():
    # 2026-06-24: OpenRouter is the preferred default. With both an OpenRouter key
    # and another provider key present, 'first provider with a key' picks openrouter.
    assert providers_with_keys(
        {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b", "OPENROUTER_API_KEY": "d"}
    )[0] == "openrouter"


def test_multikey_fallback_order_changed_vs_legacy():
    # deepseek+openrouter: PROFILES order picks openrouter first (legacy _KEY_TO_PROVIDER
    # picked deepseek). Intended: deepseek direct client is disabled.
    assert providers_with_keys(
        {"DEEPSEEK_API_KEY": "x", "OPENROUTER_API_KEY": "y"}
    )[0] == "openrouter"


def test_empty_env_returns_empty():
    assert providers_with_keys({}) == []


def test_blank_value_is_not_present():
    assert providers_with_keys({"ANTHROPIC_API_KEY": ""}) == []


# --- initializable oracle (the gating SSOT) ------------------------------------

def test_only_deepseek_is_non_initializable():
    for name, prof in PROFILES.items():
        assert prof.initializable is (name != "deepseek"), name


def test_initializable_excludes_deepseek_only():
    assert initializable_providers_with_keys({"DEEPSEEK_API_KEY": "v"}) == []


def test_initializable_keeps_real_provider_but_drops_deepseek():
    assert initializable_providers_with_keys(
        {"OPENROUTER_API_KEY": "y", "DEEPSEEK_API_KEY": "x"}
    ) == ["openrouter"]


def test_raw_oracle_still_includes_deepseek_for_display():
    # The DISPLAY oracle is unchanged — a deepseek key IS present, just not usable alone.
    assert set(providers_with_keys({"DEEPSEEK_API_KEY": "x"})) == {"deepseek"}


def test_initializable_client_set_matches_legacy_hardcoded_list():
    # LLMManager._initialize derives clients_to_try from this — guard against drift
    # from the historical hardcoded ['anthropic','openai','gemini','openrouter','nvidia'].
    derived = [p.name for p in PROFILES.values() if p.initializable]
    assert set(derived) == {"anthropic", "openai", "gemini", "openrouter", "nvidia"}
    assert "deepseek" not in derived


def test_no_key_message_steers_deepseek_via_openrouter():
    msg = no_key_message()
    assert "deepseek/deepseek-chat" in msg
    assert "OPENROUTER_API_KEY" in msg
    assert "polyrob init" in msg
