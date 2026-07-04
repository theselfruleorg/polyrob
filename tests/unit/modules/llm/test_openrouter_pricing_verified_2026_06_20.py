"""Regression: OpenRouter model pricing must match the live OpenRouter models API.

The registry is the declared SSOT for pricing (CLAUDE.md), and cost telemetry +
credit billing both flow from ``ModelPricing``. During the 2026-06-20 multi-model
live-test the registry prices were found materially WRONG for every OpenRouter
model we run — grok-4.3 was ~5x too low ($0.20/$0.50 vs the real $1.25/$2.50),
which made it look far cheaper than it bills. These tests pin the values fetched
from ``GET https://openrouter.ai/api/v1/models`` on 2026-06-20 (prices are
USD per 1M tokens) so a stale price can't silently return.
"""
from modules.llm.model_registry import get_model_config

# (input_price, output_price) per 1M tokens — verified against the live
# OpenRouter models API on 2026-06-20.
VERIFIED_PRICING = {
    "z-ai/glm-5.2": (1.20, 4.10),
    "x-ai/grok-4.3": (1.25, 2.50),
    "qwen/qwen3-235b-a22b": (0.455, 1.82),
    "qwen/qwen3-max": (0.78, 3.90),
}


def test_openrouter_model_pricing_matches_verified_values():
    for model, (pin, pout) in VERIFIED_PRICING.items():
        cfg = get_model_config(model)
        assert cfg is not None, f"{model} not registered"
        assert cfg.pricing is not None, f"{model} has no pricing"
        assert cfg.pricing.input_price == pin, (
            f"{model} input_price {cfg.pricing.input_price} != verified {pin}")
        assert cfg.pricing.output_price == pout, (
            f"{model} output_price {cfg.pricing.output_price} != verified {pout}")
