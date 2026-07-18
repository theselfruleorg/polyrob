"""Regression: pricing/registration for the 2026-07-14 model-registry refresh.

The registry is the declared SSOT for pricing (CLAUDE.md); cost telemetry + credit
billing flow from ``ModelPricing``. This pins the in/out (and cache-read) prices
fetched from ``GET https://openrouter.ai/api/v1/models`` on 2026-07-14 (USD per 1M
tokens) for the OpenRouter models added or refreshed that day — the new Grok
flagship (4.5), Grok 4.20, the new GLM tiers (5.1 / 5-turbo / 4.7-flash), and the
re-verified GLM-5.2 price — so a stale/guessed price can't silently return (the bug
class the 2026-06-20 run fixed).
"""
from modules.llm.model_registry import get_model_config, ModelProvider

# (input_price, cached_input_price, output_price) per 1M tokens — verified against
# the live OpenRouter models API on 2026-07-14.
VERIFIED_PRICING = {
    # new Grok tiers
    "x-ai/grok-4.5": (2.00, 0.50, 6.00),
    "x-ai/grok-4.20": (1.25, 0.20, 2.50),
    # new GLM tiers
    "z-ai/glm-5.1": (0.966, 0.1794, 3.036),
    "z-ai/glm-5-turbo": (1.20, 0.24, 4.00),
    "z-ai/glm-4.7-flash": (0.06, 0.01, 0.40),
    # re-verified (price drifted since the 2026-06-20 snapshot: was 1.20/4.10)
    "z-ai/glm-5.2": (0.93, 0.18, 3.00),
}

# context_window re-verified 2026-07-14 (OpenRouter API authoritative).
VERIFIED_CONTEXT = {
    "x-ai/grok-4.5": 500000,
    "x-ai/grok-4.20": 2000000,
    "x-ai/grok-4.3": 1000000,  # was 2M in the registry; live API now reports 1M
    "z-ai/glm-5.1": 202752,
    "z-ai/glm-5-turbo": 262144,
    "z-ai/glm-4.7-flash": 202752,
}


def test_refreshed_openrouter_models_registered_with_provider():
    for model in VERIFIED_PRICING:
        cfg = get_model_config(model)
        assert cfg is not None, f"{model} not registered"
        assert cfg.provider == ModelProvider.OPENROUTER, (
            f"{model} provider {cfg.provider} != OPENROUTER")


def test_refreshed_openrouter_pricing_matches_verified_values():
    for model, (pin, pcache, pout) in VERIFIED_PRICING.items():
        cfg = get_model_config(model)
        assert cfg is not None and cfg.pricing is not None, f"{model} missing"
        assert cfg.pricing.input_price == pin, (
            f"{model} input_price {cfg.pricing.input_price} != verified {pin}")
        assert cfg.pricing.output_price == pout, (
            f"{model} output_price {cfg.pricing.output_price} != verified {pout}")
        assert cfg.pricing.cached_input_price == pcache, (
            f"{model} cached_input_price {cfg.pricing.cached_input_price} != verified {pcache}")


def test_refreshed_openrouter_context_windows():
    for model, ctx in VERIFIED_CONTEXT.items():
        cfg = get_model_config(model)
        assert cfg is not None, f"{model} not registered"
        assert cfg.context_window == ctx, (
            f"{model} context_window {cfg.context_window} != verified {ctx}")


def test_grok_fallback_targets_live_flagship_not_404_model():
    # An unknown grok id must resolve to a LIVE grok, never the 404'd grok-4.1-fast.
    cfg = get_model_config("x-ai/grok-99-imaginary")
    assert cfg is not None
    assert cfg.provider == ModelProvider.OPENROUTER
    assert cfg.name == "x-ai/grok-4.5"
