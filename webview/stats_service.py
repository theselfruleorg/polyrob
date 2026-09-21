from __future__ import annotations

"""Per-call cost estimation for the console's live feed.

⚠️ 043 A30: this module used to be 612 lines of per-session feed aggregation
behind ``compute_session_stats`` and ``GET /api/session/{id}/stats``. The route
had exactly one caller — ``static/js/stats.js``, which no template loads — so
both are deleted. What SURVIVES is the cost helper, because it has a live
caller: ``webview/server.py::_enrich_llm_event_with_cost`` stamps a
``cost_estimate`` onto every ``llm_request`` feed event that arrives without
one, on the socket path the transcript actually renders.

Pricing itself is NOT here. ``modules/credits/cost_utils.py`` is the one
calculator; these two functions are the thin, lazily-imported wrappers the
console calls (a module-level import of ``modules.credits`` pulls in
``core.bot`` and closes an import cycle).
"""

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cost calculation utilities - using centralized modules/credits/cost_utils.py
# ---------------------------------------------------------------------------

# NOTE: We use lazy imports inside functions to avoid circular imports
# The stats_service runs in webview context which doesn't need core/bot/etc.
# Importing modules.credits.cost_utils at module level triggers a circular import chain:
#   modules -> modules.base_module -> core.base_component -> core -> core.bot ...


def _get_cost_utils():
    """Lazy import to avoid circular imports."""
    from modules.credits.cost_utils import calculate_cost_from_tokens, get_cost_breakdown
    return calculate_cost_from_tokens, get_cost_breakdown


def _calculate_user_cost_from_api_cost(api_cost_usd: float) -> dict:
    """
    Calculate what user pays from API cost.

    Uses centralized cost_utils for consistent pricing across the app.
    This is a FALLBACK for legacy telemetry events without user_cost_usd.

    Args:
        api_cost_usd: What we pay the API provider

    Returns:
        Dict with complete breakdown
    """
    _, get_cost_breakdown = _get_cost_utils()
    return get_cost_breakdown(api_cost_usd)


def _calculate_cost_from_registry(
    model_name: str | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None
) -> float:
    """
    Calculate cost using centralized cost_utils.

    Wrapper for backward compatibility - delegates to cost_utils.

    Args:
        model_name: Name of the model
        prompt_tokens: Number of input tokens (preferred)
        completion_tokens: Number of output tokens (preferred)
        total_tokens: Total token count (fallback)

    Returns:
        Estimated cost in USD
    """
    calculate_cost_from_tokens, _ = _get_cost_utils()
    return calculate_cost_from_tokens(
        model_name=model_name or "unknown",
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=total_tokens
    )
