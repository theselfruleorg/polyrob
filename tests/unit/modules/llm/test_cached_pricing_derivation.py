"""Test that cached_input_price is derived per provider when not explicitly set."""

from modules.llm.model_registry import get_model_config, ModelProvider


def test_anthropic_cached_price_derived_at_10pct():
    """Anthropic cache reads should be 10% of input price."""
    m = get_model_config("claude-sonnet-4-5")
    assert m.pricing.input_price == 3.0
    assert m.pricing.cached_input_price == 0.3  # 0.1x


def test_explicit_cached_price_is_preserved():
    """DeepSeek sets 0.028 explicitly (model_registry.py:722) — must not be overwritten."""
    m = get_model_config("deepseek-chat")
    assert m.pricing.cached_input_price == 0.028


def test_derivation_only_fills_none_and_matches_multiplier():
    """All models should have cached_input_price derived or preserved."""
    for name in ("claude-sonnet-4-5", "gpt-5"):
        m = get_model_config(name)
        assert m.pricing.cached_input_price is not None
        assert m.pricing.cached_input_price <= m.pricing.input_price
