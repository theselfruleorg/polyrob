"""Pricing transparency API endpoints."""

from fastapi import APIRouter
from typing import Dict, Any
import logging

# Import pricing from credits module (SINGLE SOURCE OF TRUTH)
from modules.credits.pricing import pricing as _pricing_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/models")
async def get_model_pricing():
    """
    Get current model pricing (public endpoint).

    Shows token-based pricing for all models with examples.
    """
    from modules.llm.model_registry import get_all_models, calculate_cost

    models_data = []
    all_models = get_all_models(include_deprecated=False)

    for model in all_models:
        if not model.pricing:
            continue

        # Calculate example costs
        examples = {}
        for size, (inp, out) in [("small", (1000, 500)), ("medium", (10000, 5000)), ("large", (50000, 20000))]:
            api_cost = calculate_cost(model.name, inp, out)
            credits, user_cost = _pricing_config.calculate_credits_from_api_cost(api_cost)
            examples[size] = {
                "input_tokens": inp,
                "output_tokens": out,
                "api_cost_usd": round(api_cost, 6),
                "credits": credits,
                "user_cost_usd": round(user_cost, 6)
            }

        models_data.append({
            "name": model.name,
            "provider": model.provider.value,
            "input_price_per_1M": model.pricing.input_price,
            "output_price_per_1M": model.pricing.output_price,
            "cached_price_per_1M": model.pricing.cached_input_price,
            "examples": examples
        })

    markup_info = _pricing_config.get_markup_info()
    return {
        "pricing_model": "token-based",
        "credit_value_usd": markup_info["credit_value_usd"],
        "markup": markup_info["markup"],
        "markup_percent": markup_info["markup_percentage"],
        "minimum_charge": markup_info["min_charge"],
        "description": markup_info["description"],
        "models": models_data
    }


@router.get("/calculator")
async def cost_calculator(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0
):
    """Calculate cost for specific usage."""
    from modules.llm.model_registry import calculate_cost

    try:
        api_cost = calculate_cost(model, input_tokens, output_tokens, cached_tokens)
        credits, user_cost = _pricing_config.calculate_credits_from_api_cost(api_cost)

        return {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "api_cost_usd": round(api_cost, 6),
            "markup": _pricing_config.MARKUP,
            "credits": credits,
            "user_cost_usd": round(user_cost, 6)
        }
    except Exception as e:
        return {"error": str(e)}
