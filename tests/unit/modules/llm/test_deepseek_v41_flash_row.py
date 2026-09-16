"""Regression: the DeepSeek V4.1 Flash row (prod's serving model from 2026-09-16).

Prod moved off the z.ai coding plan when it returned 429 (quota exhausted) and onto
``deepseek/deepseek-v4.1-flash`` via OpenRouter. Without a registry row the lookup
falls through ``ModelRegistry.get_model``'s pattern fallback, which maps anything
containing "deepseek" to ``deepseek/deepseek-chat`` — a DIFFERENT, older model with
1/6th the context and no vision. That substitution is silent, so it is pinned here.

Values verified against ``GET https://openrouter.ai/api/v1/models`` on 2026-09-16.
"""
from modules.llm.model_registry import get_model_config, ModelProvider

SLUG = "deepseek/deepseek-v4.1-flash"


def test_row_exists_and_is_openrouter():
    cfg = get_model_config(SLUG)
    assert cfg is not None, f"{SLUG} not registered — lookups silently fall back to deepseek-chat"
    assert cfg.name == SLUG, f"{SLUG} resolved to {cfg.name}"
    assert cfg.provider == ModelProvider.OPENROUTER


def test_no_silent_fallback_to_deepseek_chat():
    # The failure this row prevents: the pattern fallback substituting another model.
    assert get_model_config(SLUG).name != "deepseek/deepseek-chat"


def test_pricing_is_the_verified_offpeak_rate():
    # Off-peak / headline rate. Weekday 01-04 and 06-10 UTC it doubles; the row
    # documents that, and the figures here are the base the API reports.
    p = get_model_config(SLUG).pricing
    assert (p.input_price, p.output_price) == (0.15, 0.60)
    assert p.cached_input_price == 0.003


def test_capabilities_context_and_vision():
    cfg = get_model_config(SLUG)
    assert cfg.context_window == 1048576
    assert cfg.max_completion_tokens == 384000
    caps = cfg.capabilities
    # Vision is the one that changes agent behaviour: on the previous serving model
    # an inbound photo reached the agent as a literal [IMAGE] placeholder.
    assert caps.supports_vision is True
    assert caps.supports_tools and caps.supports_function_calling


def test_aliases_resolve():
    for alias in ("deepseek-v4.1-flash", "or-deepseek-v4.1-flash", "deepseek-4.1-flash"):
        assert get_model_config(alias).name == SLUG


def test_v4_flash_row_untouched():
    # Adding 4.1 must not steal the older row's name or its established alias.
    assert get_model_config("deepseek/deepseek-v4-flash").name == "deepseek/deepseek-v4-flash"
    assert get_model_config("deepseek-flash").name == "deepseek/deepseek-v4-flash"
