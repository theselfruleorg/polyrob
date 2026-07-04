"""
Shared cost calculation utilities.

SINGLE SOURCE OF TRUTH for cost estimation from token counts.
All modules should import from here, not duplicate logic.

This module consolidates:
- webview/stats_service.py::_calculate_cost_from_registry (REMOVED)
- agents/task/telemetry/service.py::_calculate_cost_from_registry (REMOVED)
- modules/llm/token_counter.py::estimate_cost (delegates here)
"""

from typing import Optional, Tuple, Dict, Any
import logging

logger = logging.getLogger(__name__)


def calculate_cost_from_tokens(
    model_name: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cached_tokens: int = 0
) -> float:
    """
    Calculate API cost from token counts using model registry.

    This is the SINGLE implementation - all other files should import this.

    Args:
        model_name: Name of the model
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/completion tokens
        total_tokens: Total tokens (fallback if split not available)
        cached_tokens: Number of cached tokens

    Returns:
        Estimated API cost in USD
    """
    from modules.llm.model_registry import calculate_cost

    if not model_name or model_name == "unknown":
        logger.debug("Cannot calculate cost: invalid model name")
        return 0.0

    # Use split tokens if available (most accurate)
    if input_tokens is not None and output_tokens is not None:
        try:
            return calculate_cost(
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens
            )
        except Exception as e:
            logger.warning(f"Cost calculation failed for {model_name}: {e}")
            # Fall through to estimation

    # Fallback: estimate split from total (65% input, 35% output)
    if total_tokens and total_tokens > 0:
        estimated_input = int(total_tokens * 0.65)
        estimated_output = int(total_tokens * 0.35)

        logger.debug(
            f"Estimating token split for {model_name}: "
            f"{estimated_input} input + {estimated_output} output = {total_tokens} total"
        )

        try:
            return calculate_cost(
                model_name=model_name,
                input_tokens=estimated_input,
                output_tokens=estimated_output,
                cached_tokens=0  # Can't estimate cached from total
            )
        except Exception as e:
            logger.warning(f"Cost estimation failed for {model_name}: {e}")

    logger.debug(f"Cannot calculate cost for {model_name}: no token data")
    return 0.0


def calculate_user_cost(api_cost_usd: float) -> Tuple[int, float]:
    """
    Calculate what user pays from API cost.

    SINGLE SOURCE - uses pricing.py configuration.

    Args:
        api_cost_usd: What we pay the API provider

    Returns:
        Tuple of (credits_charged, user_cost_usd)
    """
    from modules.credits.pricing import pricing
    return pricing.calculate_credits_from_api_cost(api_cost_usd)


def get_cost_breakdown(api_cost_usd: float) -> Dict[str, Any]:
    """
    Get complete cost breakdown for transparency.

    Args:
        api_cost_usd: What we pay the API provider

    Returns:
        Dict with complete breakdown including markup
    """
    from modules.credits.pricing import pricing

    credits_charged, user_cost_usd = pricing.calculate_credits_from_api_cost(api_cost_usd)

    return {
        "api_cost_usd": api_cost_usd,
        "user_cost_usd": user_cost_usd,
        "credits_charged": credits_charged,
        "markup": pricing.MARKUP,
        "markup_usd": user_cost_usd - api_cost_usd
    }


# Re-export for convenience
__all__ = [
    'calculate_cost_from_tokens',
    'calculate_user_cost',
    'get_cost_breakdown'
]

