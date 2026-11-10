"""Registry coverage for the current Anthropic lineup (added 2026-07-14).

The registry had been a full generation behind on Anthropic (only the 4.5 family
+ opus-4.1). This pins the newly-added modern models — Fable 5, Opus 4.8/4.7/4.6,
Sonnet 5/4.6 — with the prices from the claude-api model catalog (cached
2026-06-24, USD per 1M tokens), their 1M native context, and the invariant that
matters for the client: these models use ADAPTIVE thinking, so the registry must
NOT hand the Anthropic client a `budget_tokens` (rejected with a 400 on
Fable 5 / Opus 4.7-4.8 / Sonnet 5) — get_thinking_config() must return {}.
"""
import pytest

from modules.llm.model_registry import (
    get_all_models,
    get_model_config,
    get_model_config_exact,
    get_thinking_config,
    ModelProvider,
)
from modules.llm.llm_client_registry import DEFAULT_MODELS

# name -> (input_price, output_price) per 1M tokens.
CURRENT_ANTHROPIC = {
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-4-6": (3.00, 15.00),
}


def test_current_anthropic_models_registered():
    for name, (pin, pout) in CURRENT_ANTHROPIC.items():
        cfg = get_model_config(name)
        assert cfg is not None and cfg.name == name, f"{name} not registered"
        assert cfg.provider == ModelProvider.ANTHROPIC
        assert cfg.context_window == 1000000, f"{name} ctx {cfg.context_window}"
        assert cfg.pricing.input_price == pin, f"{name} in {cfg.pricing.input_price}"
        assert cfg.pricing.output_price == pout, f"{name} out {cfg.pricing.output_price}"
        assert cfg.capabilities.supports_thinking is True


def test_current_anthropic_uses_adaptive_thinking_not_budget_tokens():
    # budget_tokens is rejected (400) on Fable 5 / Opus 4.7-4.8 / Sonnet 5 and the 5.5 rows,
    # and deprecated on Opus 4.6 / Sonnet 4.6: the registry must not send one.
    for name in CURRENT_ANTHROPIC:
        assert "budget_tokens" not in get_thinking_config(name), name


def test_a_budget_lives_only_on_a_row_older_than_4_6():
    from modules.llm.model_registry import get_registry
    rows = [c for c in get_registry().list_models()
            if c.provider.value == "anthropic" and c.capabilities.thinking_budget_tokens]
    assert {c.name for c in rows} <= {"claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5"}


def test_the_5_5_rows_name_their_effort_and_the_history_check():
    for name in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        caps = get_model_config(name).capabilities
        assert caps.reasoning_effort == "medium" and caps.preserves_thinking
    assert get_model_config("claude-fable-5-1").capabilities.preserves_thinking
    for name in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        assert not get_model_config(name).capabilities.preserves_thinking


def test_anthropic_aliases_resolve():
    for alias, canonical in (
        ("claude-opus-4.8", "claude-opus-4-8"),
        ("opus-4.8", "claude-opus-4-8"),
        ("sonnet-5", "claude-sonnet-5"),
        ("claude-sonnet-4.6", "claude-sonnet-4-6"),
        ("fable", "claude-fable-5"),
        ("opus-5", "claude-opus-5"),
        ("claude-5-opus", "claude-opus-5"),
        ("fable-5.1", "claude-fable-5-1"),
        ("claude-fable-5.1", "claude-fable-5-1"),
        ("claude-fable", "claude-fable-5"),
    ):
        cfg = get_model_config(alias)
        assert cfg is not None and cfg.name == canonical, f"{alias} -> {cfg and cfg.name}"


#: F9 (2026-09-23): the models that accept mid-conversation tool changes
#: (`defer_loading` + `tool_addition`, beta mid-conversation-tool-changes-
#: 2026-07-01). This is the ONE table the deferred late-tool mode reads, so a
#: name-pattern guess can never creep back in.
DEFERRED_TOOLS_CAPABLE = ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                          "claude-fable-5", "claude-fable-5-1", "claude-sonnet-5-5")
DEFERRED_TOOLS_INCAPABLE = (
    "claude-opus-4-7", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-haiku-4-5", "claude-haiku-5-5",
)


def test_mid_conversation_tool_changes_capability():
    for name in DEFERRED_TOOLS_CAPABLE:
        cfg = get_model_config(name)
        assert cfg.capabilities.supports_mid_conversation_tool_changes is True, name
    for name in DEFERRED_TOOLS_INCAPABLE:
        cfg = get_model_config(name)
        assert cfg.capabilities.supports_mid_conversation_tool_changes is False, name


def test_fable_5_1_cache_read_price_is_explicit():
    """$0.25/MTok, NOT the 0.1x the Anthropic derivation would produce."""
    assert get_model_config("claude-fable-5-1").pricing.cached_input_price == 0.25
    assert get_model_config("claude-fable-5-1").max_completion_tokens == 128000


def test_anthropic_default_is_a_model_that_is_not_retiring():
    assert DEFAULT_MODELS["anthropic"] == "claude-sonnet-5-5"
    assert get_model_config_exact("claude-sonnet-5-5").deprecated is False


def test_anthropic_oauth_seat_serves_the_current_lineup():
    from modules.llm.provider_spec import get_spec
    spec = get_spec("anthropic-oauth")
    assert spec.default_model == "claude-sonnet-5-5"
    for name in spec.models:
        assert get_model_config_exact(name).deprecated is False, name


def test_every_claude_row_sees_images():
    rows = get_all_models(ModelProvider.ANTHROPIC, include_deprecated=True)
    assert rows
    assert [r.name for r in rows if not r.capabilities.supports_vision] == []


def test_docs_prices_of_the_new_lineup():
    assert get_model_config_exact("claude-opus-5-5").pricing.cached_input_price == 0.20
    assert get_model_config_exact("claude-opus-5-5").max_completion_tokens == 128000
    assert get_model_config_exact("claude-sonnet-5-5").pricing.cached_input_price == 0.20
    assert get_model_config_exact("claude-sonnet-5-5").max_completion_tokens == 128000
    assert get_model_config_exact("claude-haiku-5-5").pricing.cached_input_price == 0.01
    assert get_model_config_exact("claude-fable-5").pricing.cached_input_price == 1.00


@pytest.mark.parametrize("retired", [
    "claude-3-5-sonnet-20241022", "claude-3-5-sonnet", "claude-3-sonnet", "claude-sonnet-4",
    "claude-3-5-haiku", "claude-3-haiku", "claude-3-opus", "claude-opus-4-1", "claude-opus-4",
])
def test_retired_ids_have_no_exact_row(retired):
    """A retired id must not pass the price lookup: it is a 404 on the wire."""
    assert get_model_config_exact(retired) is None
