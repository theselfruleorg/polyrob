"""Registry coverage for the current Anthropic lineup (added 2026-07-14).

The registry had been a full generation behind on Anthropic (only the 4.5 family
+ opus-4.1). This pins the newly-added modern models — Fable 5, Opus 4.8/4.7/4.6,
Sonnet 5/4.6 — with the prices from the claude-api model catalog (cached
2026-06-24, USD per 1M tokens), their 1M native context, and the invariant that
matters for the client: these models use ADAPTIVE thinking, so the registry must
NOT hand the Anthropic client a `budget_tokens` (rejected with a 400 on
Fable 5 / Opus 4.7-4.8 / Sonnet 5) — get_thinking_config() must return {}.
"""
from modules.llm.model_registry import (
    get_model_config,
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
    "claude-sonnet-5": (3.00, 15.00),
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
    # budget_tokens is rejected (400) on Fable 5 / Opus 4.7-4.8 / Sonnet 5 and
    # deprecated on Opus 4.6 / Sonnet 4.6 — the registry must not send one, so
    # get_thinking_config() stays empty (byte-identical to pre-2026-07-14).
    for name in CURRENT_ANTHROPIC:
        assert get_thinking_config(name) == {}, (
            f"{name} must not emit thinking params (adaptive-only): "
            f"{get_thinking_config(name)}")


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
#: 2026-07-01). Claude Opus 5 ONWARD — this is the ONE table the deferred
#: late-tool mode reads, so a name-pattern guess can never creep back in.
DEFERRED_TOOLS_CAPABLE = ("claude-opus-5", "claude-fable-5", "claude-fable-5-1")
DEFERRED_TOOLS_INCAPABLE = (
    "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-haiku-4-5",
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


def test_anthropic_default_unchanged():
    # Policy: adding the new models did NOT move the anthropic default.
    assert DEFAULT_MODELS["anthropic"] == "claude-sonnet-4-5"
