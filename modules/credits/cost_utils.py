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
    cached_tokens: int = 0,
    cache_creation_tokens: int = 0
) -> float:
    """
    Calculate API cost from token counts using model registry.

    This is the SINGLE implementation - all other files should import this.

    ⚠️ ESTIMATE PATH, NOT THE BILLING PATH: this helper (and its callers --
    telemetry display estimates, webview stats, the public pricing
    calculator) feeds DISPLAY numbers only. The real charge/deduction and
    the `usage_records` ledger row are computed by
    `LLMUsageTracker._calculate_costs` (which now routes through
    `modules.credits.pricing.compute_llm_cost`, the actual billing entry
    point). If you're wiring up a new billing-affecting call site, use
    `compute_llm_cost` directly instead of this function.

    Args:
        model_name: Name of the model
        input_tokens: Number of input/prompt tokens
        output_tokens: Number of output/completion tokens
        total_tokens: Total tokens (fallback if split not available)
        cached_tokens: Number of cached (read) tokens
        cache_creation_tokens: Number of cache-WRITE tokens (G-24: Anthropic
            1.25x surcharge). Defaults to 0 for callers that genuinely can't
            know this (e.g. the total-tokens-only estimate fallback below,
            or a caller with no cache-metrics from its provider) -- that is
            a correct default, not a dropped parameter, as long as the
            caller forwards whatever it DOES have.

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
                cached_tokens=cached_tokens,
                cache_creation_tokens=cache_creation_tokens
            )
        except Exception as e:
            logger.warning(f"Cost calculation failed for {model_name}: {e}")
            # Fall through to estimation

    # Fallback: estimate split from total (65% input, 35% output). This is a
    # genuine "can't know cache tokens" case -- a bare total-token count
    # carries no cache-read/cache-write split at all, so 0/0 here is not a
    # dropped parameter, it's the honest ceiling of what this estimate can do.
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
                cached_tokens=0,  # Can't estimate cached from total
                cache_creation_tokens=0  # Can't estimate cache-write from total either
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

