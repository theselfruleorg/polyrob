"""The one price rule for a model change: ``/model`` quotes first, a fallback does not move.

Pure and with no import, so the LLM manager reads the rule with no import cycle.
"""
from typing import Optional, Tuple

#: A model that costs more than this many times the current one (input or output price).
GUARD_RATIO = 5.0
#: …and any model above this price (USD per million output tokens).
GUARD_OUTPUT_USD_PER_M = 30.0


def guard_reason(cur_price: Optional[Tuple[float, float]],
                 new_price: Optional[Tuple[float, float]]) -> str:
    """Why a switch must be quoted first (``""`` = no quote). Prices are
    ``(input, output)`` USD per million tokens; an unknown price never quotes."""
    if not new_price:
        return ""
    n_in, n_out = new_price
    if n_out > GUARD_OUTPUT_USD_PER_M:
        return f"it costs ${n_out:g} per million output tokens"
    if cur_price:
        c_in, c_out = cur_price
        for label, cur, new in (("input", c_in, n_in), ("output", c_out, n_out)):
            if cur > 0 and new / cur > GUARD_RATIO:
                return f"its {label} price is {new / cur:.0f}x the current model's"
    return ""
