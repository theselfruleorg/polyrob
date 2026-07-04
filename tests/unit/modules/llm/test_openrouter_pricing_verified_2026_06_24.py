"""Regression: pricing/registration for the 2026-06-24 OpenRouter shortlist expansion.

The registry is the declared SSOT for pricing (CLAUDE.md); cost telemetry + credit
billing flow from ``ModelPricing``. This pins the in/out prices fetched from
``GET https://openrouter.ai/api/v1/models`` on 2026-06-24 (USD per 1M tokens) for
the 15 newly-curated OpenRouter models, so a stale/guessed price can't silently
return (the bug class the 2026-06-20 run fixed). It also asserts each slug resolves
via ``get_model_config`` and reports the OpenRouter provider.
"""
from modules.llm.model_registry import get_model_config, ModelProvider

# (input_price, output_price) per 1M tokens — verified against the live OpenRouter
# models API on 2026-06-24.
VERIFIED_PRICING = {
    # frontier agentic / coding
    "moonshotai/kimi-k2.5": (0.375, 2.025),
    "minimax/minimax-m2": (0.255, 1.0),
    "z-ai/glm-4.6": (0.43, 1.74),
    "qwen/qwen3-coder-30b-a3b-instruct": (0.07, 0.27),
    # cheap high-volume workhorses
    "deepseek/deepseek-v3.2": (0.2288, 0.3432),
    "deepseek/deepseek-v4-flash": (0.09, 0.18),
    "meta-llama/llama-3.3-70b-instruct": (0.1, 0.32),
    "mistralai/mistral-small-3.2-24b-instruct": (0.075, 0.2),
    "qwen/qwen3-30b-a3b-instruct-2507": (0.04815, 0.193),
    # open-weights / OSS
    "openai/gpt-oss-120b": (0.039, 0.18),
    "nousresearch/hermes-4-70b": (0.13, 0.4),
    "nousresearch/hermes-4-405b": (1.0, 3.0),
    # vision / long-context
    "qwen/qwen3-vl-8b-instruct": (0.08, 0.5),
    "meta-llama/llama-4-scout": (0.1, 0.3),
    "minimax/minimax-m3": (0.3, 1.2),
}


def test_new_openrouter_models_registered_with_provider():
    for model in VERIFIED_PRICING:
        cfg = get_model_config(model)
        assert cfg is not None, f"{model} not registered"
        assert cfg.provider == ModelProvider.OPENROUTER, (
            f"{model} provider {cfg.provider} != OPENROUTER")


def test_new_openrouter_model_pricing_matches_verified_values():
    for model, (pin, pout) in VERIFIED_PRICING.items():
        cfg = get_model_config(model)
        assert cfg is not None and cfg.pricing is not None, f"{model} missing"
        assert cfg.pricing.input_price == pin, (
            f"{model} input_price {cfg.pricing.input_price} != verified {pin}")
        assert cfg.pricing.output_price == pout, (
            f"{model} output_price {cfg.pricing.output_price} != verified {pout}")


def test_vision_capable_models_flagged():
    # These four expose image input on OpenRouter — supports_vision must be True so
    # the agent's vision path is offered for them.
    for model in ("mistralai/mistral-small-3.2-24b-instruct",
                  "qwen/qwen3-vl-8b-instruct", "meta-llama/llama-4-scout",
                  "minimax/minimax-m3"):
        cfg = get_model_config(model)
        assert cfg.capabilities.supports_vision is True, f"{model} should be vision"


def test_hermes_models_have_no_native_tools():
    # Hermes-4 on OpenRouter does NOT advertise the `tools` parameter; mark them
    # tool-less so the agent uses the JSON-from-text fallback rather than emitting
    # native tool calls the endpoint will reject.
    for model in ("nousresearch/hermes-4-70b", "nousresearch/hermes-4-405b"):
        cfg = get_model_config(model)
        assert cfg.capabilities.supports_tools is False, f"{model} tools should be False"
