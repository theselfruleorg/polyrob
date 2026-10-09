"""Conservative USD accounting precision shared by the transaction rails."""
import math


def usd_ceiling(amount) -> float:
    value = float(amount)
    if not math.isfinite(value) or value < 0:
        raise ValueError('USD valuation must be finite and nonnegative')
    # Ignore only binary-float noise at an exact cent. Every positive amount,
    # including dust smaller than that tolerance, consumes at least one cent.
    return max(0.01, math.ceil(value * 100 - 1e-9) / 100) if value else 0.0
