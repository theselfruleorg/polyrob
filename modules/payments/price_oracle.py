"""Live price oracle for the deposit monitor (C8).

One function POLYROB actually calls. Default implementation fetches from
CoinGecko's public simple-price API (no key required). Fails CLOSED on error
(raises) rather than silently returning a stale/wrong price — DepositMonitor
uses this to compute how many credits to grant a real deposit; a wrong number
would misprice actual money, which is worse than a temporarily-skipped check.
"""
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# Sanity upper bound on the ETH/USD price (C8 money-safety review): guards
# against a schema hiccup in the CoinGecko response or an operator typo in
# `ETH_PRICE_USD_OVERRIDE` (an extra digit) turning into a wildly-inflated
# credit grant. The downward direction is already self-defended by
# DepositMonitor's `min_deposit_usd` floor (a too-low/zero/negative price
# just makes `amount_usd` fail that floor), so only an upper clamp is added
# here. Configurable via `ETH_PRICE_USD_MAX` for operators who need headroom
# above the default as real ETH prices move over time.
_DEFAULT_MAX_ETH_PRICE_USD = 50_000.0


async def get_eth_price_usd(http_client: Optional[Any] = None) -> float:
    """Fetch the live ETH/USD price.

    Args:
        http_client: injectable async client exposing an `async get(url, params=...)`
            coroutine returning an object with `.raise_for_status()` and `.json()`
            (matches the subset of the `httpx.AsyncClient` interface this module
            uses). Defaults to a real `httpx.AsyncClient` when omitted.

    Raises:
        Exception: on any network/parse failure, a non-2xx response, or a
            price outside the plausible sanity range — by design (see module
            docstring). Callers MUST treat a raised exception as "skip this
            cycle, retry next tick," never as "assume some price."
    """
    override = os.environ.get("ETH_PRICE_USD_OVERRIDE")
    if override:
        price = float(override)
    elif http_client is not None:
        resp = await http_client.get(_COINGECKO_URL, params={"ids": "ethereum", "vs_currencies": "usd"})
        resp.raise_for_status()
        data = resp.json()
        price = float(data["ethereum"]["usd"])
    else:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_COINGECKO_URL, params={"ids": "ethereum", "vs_currencies": "usd"})
            resp.raise_for_status()
            data = resp.json()
        price = float(data["ethereum"]["usd"])

    max_price = float(os.environ.get("ETH_PRICE_USD_MAX", _DEFAULT_MAX_ETH_PRICE_USD))
    if not (price > 0):
        raise ValueError(f"ETH price oracle returned a non-positive price: {price!r}")
    if price > max_price:
        raise ValueError(
            f"ETH price {price} exceeds sanity upper bound {max_price} "
            f"(ETH_PRICE_USD_MAX) — refusing to use it; this is likely an "
            f"oracle schema error or an ETH_PRICE_USD_OVERRIDE typo, not a "
            f"real price"
        )

    return price
