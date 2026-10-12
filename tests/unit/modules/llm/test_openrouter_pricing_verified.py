"""Regression: OpenRouter model pricing must match the live OpenRouter models API.

The registry is the declared SSOT for pricing, and cost telemetry + credit billing
both flow from ``ModelPricing``. During the 2026-06-20 multi-model live-test the
registry prices were found materially WRONG for every OpenRouter model we ran —
grok-4.3 was ~5x too low ($0.20/$0.50 vs the real $1.25/$2.50), which made it look
far cheaper than it bills. This ONE table pins the values fetched from
``GET https://openrouter.ai/api/v1/models`` (USD per 1M tokens), each row with the
day it was verified, so a stale or guessed price cannot silently return.

It replaces three dated snapshot files (2026-06-20, 2026-06-24, 2026-07-14) that
pinned disjoint rows of the same table. When a live price moves, edit the row and
its date; do not add a new dated file.
"""
from modules.llm.model_registry import ModelProvider, get_model_config

# model: (input_price, cached_input_price or None = not pinned, output_price, verified)
VERIFIED_PRICING = {
    # 2026-06-20 live-test fixes
    "x-ai/grok-4.3": (1.25, None, 2.50, "2026-06-20"),
    "qwen/qwen3-235b-a22b": (0.455, None, 1.82, "2026-06-20"),
    "qwen/qwen3-max": (0.78, None, 3.90, "2026-06-20"),
    # 2026-06-24 shortlist expansion — frontier agentic / coding
    "moonshotai/kimi-k2.5": (0.375, None, 2.025, "2026-06-24"),
    "minimax/minimax-m2": (0.255, None, 1.0, "2026-06-24"),
    "z-ai/glm-4.6": (0.43, None, 1.74, "2026-06-24"),
    "qwen/qwen3-coder-30b-a3b-instruct": (0.07, None, 0.27, "2026-06-24"),
    # cheap high-volume workhorses
    "deepseek/deepseek-v3.2": (0.2288, None, 0.3432, "2026-06-24"),
    "deepseek/deepseek-v4-flash": (0.09, None, 0.18, "2026-06-24"),
    "meta-llama/llama-3.3-70b-instruct": (0.1, None, 0.32, "2026-06-24"),
    "mistralai/mistral-small-3.2-24b-instruct": (0.075, None, 0.2, "2026-06-24"),
    "qwen/qwen3-30b-a3b-instruct-2507": (0.04815, None, 0.193, "2026-06-24"),
    # open-weights / OSS
    "openai/gpt-oss-120b": (0.039, None, 0.18, "2026-06-24"),
    "nousresearch/hermes-4-70b": (0.13, None, 0.4, "2026-06-24"),
    "nousresearch/hermes-4-405b": (1.0, None, 3.0, "2026-06-24"),
    # vision / long-context
    "qwen/qwen3-vl-8b-instruct": (0.08, None, 0.5, "2026-06-24"),
    "meta-llama/llama-4-scout": (0.1, None, 0.3, "2026-06-24"),
    "minimax/minimax-m3": (0.3, None, 1.2, "2026-06-24"),
    # 2026-07-14 registry refresh — new Grok tiers
    "x-ai/grok-4.5": (2.00, 0.50, 6.00, "2026-07-14"),
    "x-ai/grok-4.20": (1.25, 0.20, 2.50, "2026-07-14"),
    # new GLM tiers
    "z-ai/glm-5.1": (0.966, 0.1794, 3.036, "2026-07-14"),
    "z-ai/glm-5-turbo": (1.20, 0.24, 4.00, "2026-07-14"),
    "z-ai/glm-4.7-flash": (0.06, 0.01, 0.40, "2026-07-14"),
    # re-verified (was 1.20/4.10 on 2026-06-20)
    "z-ai/glm-5.2": (0.93, 0.18, 3.00, "2026-07-14"),
}

# context_window, OpenRouter API authoritative (verified 2026-07-14).
VERIFIED_CONTEXT = {
    "x-ai/grok-4.5": 500000,
    "x-ai/grok-4.20": 2000000,
    "x-ai/grok-4.3": 1000000,  # was 2M in the registry; the live API reports 1M
    "z-ai/glm-5.1": 202752,
    "z-ai/glm-5-turbo": 262144,
    "z-ai/glm-4.7-flash": 202752,
}


def test_every_pinned_model_is_registered_as_openrouter():
    for model in VERIFIED_PRICING:
        cfg = get_model_config(model)
        assert cfg is not None, f"{model} not registered"
        assert cfg.provider == ModelProvider.OPENROUTER, (
            f"{model} provider {cfg.provider} != OPENROUTER")


def test_openrouter_pricing_matches_verified_values():
    for model, (pin, pcache, pout, day) in VERIFIED_PRICING.items():
        cfg = get_model_config(model)
        assert cfg is not None and cfg.pricing is not None, f"{model} has no pricing"
        assert cfg.pricing.input_price == pin, (
            f"{model} input_price {cfg.pricing.input_price} != verified {pin} ({day})")
        assert cfg.pricing.output_price == pout, (
            f"{model} output_price {cfg.pricing.output_price} != verified {pout} ({day})")
        if pcache is not None:
            assert cfg.pricing.cached_input_price == pcache, (
                f"{model} cached_input_price {cfg.pricing.cached_input_price} "
                f"!= verified {pcache} ({day})")


def test_openrouter_context_windows():
    for model, ctx in VERIFIED_CONTEXT.items():
        cfg = get_model_config(model)
        assert cfg is not None, f"{model} not registered"
        assert cfg.context_window == ctx, (
            f"{model} context_window {cfg.context_window} != verified {ctx}")


def test_vision_capable_models_flagged():
    # These four expose image input on OpenRouter — supports_vision must be True so
    # the agent's vision path is offered for them.
    for model in ("mistralai/mistral-small-3.2-24b-instruct",
                  "qwen/qwen3-vl-8b-instruct", "meta-llama/llama-4-scout",
                  "minimax/minimax-m3"):
        cfg = get_model_config(model)
        assert cfg.capabilities.supports_vision is True, f"{model} should be vision"


def test_hermes4_models_have_no_native_tools():
    # Hermes-4 on OpenRouter does NOT advertise the `tools` parameter; mark them
    # tool-less so the agent uses the JSON-from-text fallback rather than emitting
    # native tool calls the endpoint will reject.
    for model in ("nousresearch/hermes-4-70b", "nousresearch/hermes-4-405b"):
        cfg = get_model_config(model)
        assert cfg.capabilities.supports_tools is False, f"{model} tools should be False"


def test_grok_fallback_targets_live_flagship_not_404_model():
    # An unknown grok id must resolve to a LIVE grok, never the 404'd grok-4.1-fast.
    cfg = get_model_config("x-ai/grok-99-imaginary")
    assert cfg is not None
    assert cfg.provider == ModelProvider.OPENROUTER
    assert cfg.name == "x-ai/grok-4.5"
