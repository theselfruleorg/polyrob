"""Tests for available_models() — the ONE model-list builder (task P0.5).

Joins the provider-key oracle (modules.llm.profiles) x the model registry
(modules.llm.model_registry) so every surface (CLI picker, WebView
capabilities, /v1/models, pricing) can consume ONE list instead of the three
that existed before this task.
"""
from modules.llm.available_models import available_models, ModelChoice, steer_notes


def test_lists_models_for_provider_with_key():
    env = {"OPENROUTER_API_KEY": "sk-or-" + "x" * 24}
    choices = available_models(env)
    assert choices and all(isinstance(c, ModelChoice) for c in choices)
    assert all(c.provider == "openrouter" for c in choices)
    assert any(c.is_default for c in choices), "the resolved default must be flagged"
    assert all(c.display_name for c in choices), "every choice has a display name"


def test_deepseek_key_alone_yields_no_selectable_and_a_steer_note():
    env = {"DEEPSEEK_API_KEY": "sk-" + "x" * 24}
    assert available_models(env) == []            # initializable=False -> not selectable
    notes = steer_notes(env)
    assert any("openrouter" in n.lower() for n in notes)


def test_canonical_provider_string_not_google():
    env = {"GEMINI_API_KEY": "AIza" + "x" * 24}
    choices = available_models(env)
    assert choices, "gemini models must be listed (regression guard for the google/gemini enum-string mismatch)"
    assert all(c.provider == "gemini" for c in choices)  # never "google"


def test_no_keys_yields_empty_list_and_no_steer_notes():
    env = {}
    assert available_models(env) == []
    assert steer_notes(env) == []


def test_multiple_keys_preserve_profiles_order():
    # PROFILES order is openrouter, anthropic, openai, gemini, nvidia, deepseek.
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-" + "a" * 24,
        "OPENROUTER_API_KEY": "sk-or-" + "b" * 24,
    }
    choices = available_models(env)
    seen_providers = []
    for c in choices:
        if c.provider not in seen_providers:
            seen_providers.append(c.provider)
    assert seen_providers == ["openrouter", "anthropic"]


def test_initialized_only_filters_to_the_given_provider_set():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-" + "a" * 24,
        "OPENROUTER_API_KEY": "sk-or-" + "b" * 24,
    }
    choices = available_models(env, initialized_only=True, initialized_providers={"anthropic"})
    assert choices
    assert all(c.provider == "anthropic" for c in choices)


def test_pricing_and_context_hints_are_populated():
    env = {"OPENROUTER_API_KEY": "sk-or-" + "x" * 24}
    choices = available_models(env)
    for c in choices:
        assert isinstance(c.context_window, int)
        assert isinstance(c.pricing_hint, str) and c.pricing_hint
        assert isinstance(c.supports_vision, bool)
        assert isinstance(c.supports_tools, bool)
