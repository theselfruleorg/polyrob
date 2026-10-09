"""The worst Polymarket fee an order can pay, so the spend caps charge it.

Polymarket's fee is per MARKET. The CLOB publishes it in the market info
(``GET /clob-markets/{condition_id}``):

- ``fd = {"r": rate, "e": exponent}`` — the platform fee: ``shares * r * (p * (1 - p)) ** e``
  (py-clob-client-v2 ``fees.adjust_buy_amount_for_fees``);
- ``tbf`` / ``mbf`` — the taker / maker base fee in bps: ``shares * bps / 10000 * min(p, 1 - p)``.

Both are largest at ``p = 0.5``, so the worst fill price inside the order's price band is
the band point nearest 0.5. The charge is the larger of the two formulas (the venue
applies one; which one depends on the order version, so the caps assume the dearer).

When the market's fee cannot be read, the caps are charged ``DEFAULT_FEE_RATE`` of the
notional instead — a conservative ceiling above every published Polymarket fee schedule
(the highest, crypto up/down markets, is about 3% of notional at p = 0.5). An unreadable
fee never refuses the order.
"""
import math
from typing import Any, Mapping, Optional, Tuple

#: Fraction of the order notional charged as the fee when the market's fee is unreadable.
DEFAULT_FEE_RATE = 0.05


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def worst_fee_usd(info: Optional[Mapping[str, Any]], *, shares: float, price: float,
                  reference: float, notional_usd: float) -> Tuple[float, str]:
    """Return ``(fee_usd, source)``; ``source`` is ``"market"`` or ``"default"``.

    ``info`` is the CLOB market-info dict (or None when it could not be read).
    """
    default = (float(notional_usd) * DEFAULT_FEE_RATE, "default")
    if not isinstance(info, Mapping) or not info.get("t"):   # not a real market-info reply
        return default
    low, high = sorted((float(price), float(reference)))
    worst_p = min(max(0.5, low), high)
    per_share = 0.0
    fd = info.get("fd")
    if fd is not None:
        if not isinstance(fd, Mapping):
            return default
        rate, exponent = _finite(fd.get("r", 0)), _finite(fd.get("e", 0))
        if rate is None or exponent is None:
            return default
        per_share = max(per_share, rate * (worst_p * (1 - worst_p)) ** exponent)
    for key in ("tbf", "mbf"):
        if info.get(key) is None:
            continue
        bps = _finite(info.get(key))
        if bps is None:
            return default
        per_share = max(per_share, bps / 10000 * min(worst_p, 1 - worst_p))
    fee = float(shares) * per_share
    if not math.isfinite(fee):
        return default
    return fee, "market"
