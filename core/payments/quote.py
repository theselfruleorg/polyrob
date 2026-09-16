"""Sizing a USD price into a token amount — POLICY only (046 §4.4).

`core/` may not import `tools/`, so the QUOTE arrives from the container service
``payment_quoter`` (`tools/defi/payment_quote.py`) and this module decides
whether it may be used. Same split `core/surfaces/group_admin.py` uses for
`group_ledger`.

⚠️ The attack this module exists to stop: a USD-denominated fee shrinks in TOKEN
terms as the token's price RISES, so anyone who can move a thin pool upward buys
the action for dust. The asset's ``min_amount_raw`` is the hard floor, and a pool
the screen calls WASH or UNSCREENABLE is not a price at all.

A crashed price makes the fee LARGER in token units. That only costs the payer,
and the USD side is already bounded by ``X402_INVOICE_MAX_USD``, so it is sized
rather than refused.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

#: Verdicts from `tools/defi/pool_screen.py` that are NOT a usable price.
#:
#: ⚠️ ``UNSCREENABLE`` is included deliberately. An indexer that has not caught
#: up is not a manipulator — but it is also not evidence we may charge against,
#: and the two failure modes are indistinguishable from here.
_UNUSABLE_VERDICTS = ("WASH", "UNSCREENABLE")

#: Symbols treated as dollar-pegged, needing no quote at all.
_STABLE_SYMBOLS = ("USDC", "USDT", "DAI", "PYUSD", "USDG", "USDS")


class QuoteRefused(ValueError):
    """No honest price.

    ⚠️ Always a refusal that NAMES its reason — never a fallback figure, and
    never a ``$0.00`` that reads as a free action.
    """


@dataclass(frozen=True)
class PriceQuote:
    asset_id: str
    usd_per_token: float
    liquidity_usd: float
    verdict: str
    source: str
    ts: float


def quote_max_age_sec() -> int:
    from core.env import int_env
    return max(1, int_env("PAYMENT_QUOTE_MAX_AGE_SEC", 300))


def is_stable(asset) -> bool:
    """Is this asset dollar-pegged, so a USD price needs no quote at all?"""
    return (getattr(asset, "symbol", "") or "").upper() in _STABLE_SYMBOLS


def size_amount_raw(usd: float, asset, quote: Optional[PriceQuote], *,
                    now: Optional[float] = None,
                    max_age_sec: Optional[int] = None) -> int:
    """Raw token units for *usd* of *asset*. Raises `QuoteRefused` otherwise.

    A dollar-pegged asset needs no quote. Everything else needs a FRESH quote
    over a pool the screen did not call WASH or UNSCREENABLE, deeper than the
    asset's own liquidity floor, and the result must clear ``min_amount_raw``.
    """
    if usd is None or float(usd) <= 0:
        raise QuoteRefused(f"a price of {usd!r} is not an amount anyone can pay")
    symbol = getattr(asset, "symbol", "") or getattr(asset, "asset_id", "?")
    decimals = int(getattr(asset, "decimals", 0))

    if is_stable(asset):
        raw = int((Decimal(str(usd)) * (10 ** decimals)).to_integral_value())
    else:
        if quote is None:
            raise QuoteRefused(
                f"no price available for {symbol} — refusing to size a fee "
                f"against a token I cannot value")
        age_cap = quote_max_age_sec() if max_age_sec is None else int(max_age_sec)
        if (now or time.time()) - float(quote.ts) > age_cap:
            raise QuoteRefused(
                f"the {symbol} price is stale (older than {age_cap}s)")
        if quote.verdict in _UNUSABLE_VERDICTS:
            raise QuoteRefused(
                f"the {symbol} pool screens as {quote.verdict} — that is not a "
                f"price I may charge against")
        floor_usd = float(getattr(asset, "liquidity_floor_usd", 0) or 0)
        if float(quote.liquidity_usd) < floor_usd:
            raise QuoteRefused(
                f"{symbol} liquidity ${float(quote.liquidity_usd):,.0f} is below "
                f"this asset's ${floor_usd:,.0f} floor")
        if float(quote.usd_per_token) <= 0:
            raise QuoteRefused(
                f"{symbol} priced at {quote.usd_per_token} is not a price")
        tokens = Decimal(str(usd)) / Decimal(str(quote.usd_per_token))
        raw = int((tokens * (10 ** decimals)).to_integral_value())

    if raw <= 0:
        raise QuoteRefused(
            f"${usd} of {symbol} rounds to zero raw units — refusing rather "
            f"than selling this for nothing")
    floor_raw = int(getattr(asset, "min_amount_raw", 0) or 0)
    if floor_raw and raw < floor_raw:
        raise QuoteRefused(
            f"${usd} is only {raw} raw units of {symbol}, below this asset's "
            f"floor of {floor_raw} — a rising token price cannot be allowed to "
            f"make this free")
    return raw


__all__ = ["PriceQuote", "QuoteRefused", "is_stable", "quote_max_age_sec",
           "size_amount_raw"]
