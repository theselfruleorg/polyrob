"""Token-cost math for the model registry (``calculate_cost``).

Split out of ``modules/llm/model_registry.py`` (S4, 2026-08-29); ``model_registry``
re-exports ``calculate_cost``. The billing entry point that must be used for anything
that charges is ``modules.credits.pricing.compute_llm_cost`` (it forwards BOTH cache
token classes and applies the flat-rate / untrusted-price rules); this is the shared
per-token arithmetic underneath it.
"""
import logging

logger = logging.getLogger(__name__)


def calculate_cost(model_name: str, input_tokens: int, output_tokens: int,
                  cached_tokens: int = 0, cache_creation_tokens: int = 0) -> float:
    """Calculate cost for token usage

    Args:
        model_name: Name of the model
        input_tokens: Number of input tokens (INCLUDES cached + cache-creation tokens;
            Anthropic folds cache reads/writes back into input before calling here)
        output_tokens: Number of output tokens
        cached_tokens: Number of cached input tokens (reads, discounted)
        cache_creation_tokens: Number of cache-WRITE tokens (G3: Anthropic 1.25x)

    Returns:
        Total cost in USD
    """
    from modules.llm.model_registry import get_model_config
    config = get_model_config(model_name)
    if not config or not config.pricing:
        logger.warning(f"No pricing info for {model_name}")
        return 0.0

    pricing = config.pricing

    # Regular input = everything that is neither a cache read nor a cache write.
    regular_input_tokens = input_tokens - cached_tokens - cache_creation_tokens
    if regular_input_tokens < 0:
        # Defensive: never let a mis-reported split produce negative input.
        regular_input_tokens = 0
    input_cost = (regular_input_tokens / 1_000_000) * pricing.input_price

    # Add cached input cost if applicable
    if cached_tokens > 0 and pricing.cached_input_price is not None:
        input_cost += (cached_tokens / 1_000_000) * pricing.cached_input_price

    # G3: cache-WRITE (creation) tokens billed at cache_write_price when the provider
    # surcharges them (Anthropic 1.25x); otherwise at plain input price (no surcharge).
    if cache_creation_tokens > 0:
        write_price = pricing.cache_write_price if pricing.cache_write_price is not None else pricing.input_price
        input_cost += (cache_creation_tokens / 1_000_000) * write_price

    # Calculate output cost
    output_cost = (output_tokens / 1_000_000) * pricing.output_price

    return input_cost + output_cost
