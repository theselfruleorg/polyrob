"""Composite pool verdict: is this pool's trading flow explained by its depth?

Pure and tested, reachable as a verb (``defi_data.scan``, the discovery
pre-screen, the payment-quote sanity check).

**The lines are neutral defaults, not measurements.** Each is a round,
conservative starting line an install tunes for its own chains in
``<data_home>/trading_presets.json`` under ``screen.pool``
(``core.trading_presets.screen_lines``), read at call time:

    signal                     default line
    ------------------------   -------------------------------------
    24h volume / liquidity     engage <= 5, wash > 10
    main-pool liquidity        floor $500k
    pool age at decision       under 12h too new; >= 48h confirmed
    txns per unique buyer      wash > 20
    1h volume / liquidity      wash > 5

Volume-over-liquidity is the strongest single separator: honest pools trade a
small multiple of their depth a day; wash flow trades many multiples. None of
the lines is sufficient alone.

**A verdict is never built on a missing number.** An unreadable input yields
UNSCREENABLE with the missing fields named, so an indexer that has not caught up
never reads like a manipulator. This screen also decides nothing about a
CONTRACT: honeypot, taxes, mint authority and holder concentration are
`token_info` and `token_holders`, and a SURVIVOR verdict here is not a safety
claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

#: 24h volume / liquidity. At or below this a pool is worth engaging with.
VOL_LIQ_ENGAGE_MAX = 5.0
#: Above this the flow cannot be explained by the depth: wash.
VOL_LIQ_WASH_MIN = 10.0
#: 1h volume / liquidity. Catches a wash the 24h average smears out.
HOURLY_VOL_LIQ_WASH_MIN = 5.0
#: Transactions per UNIQUE 24h buyer. Above this: bot ping-pong.
TXNS_PER_BUYER_WASH_MIN = 20.0
#: Below this depth a small ticket cannot be exited without eating the book.
LIQUIDITY_FLOOR_USD = 500_000.0
#: Under this age nothing has been proven either way.
AGE_TOO_NEW_HOURS = 12.0
#: At or over this age a pool that still looks healthy has survived something.
AGE_CONFIRMED_HOURS = 48.0

#: The ``screen.pool`` keys an install may set, and their neutral defaults.
DEFAULT_LINES = {
    "vol_liq_engage_max": VOL_LIQ_ENGAGE_MAX,
    "vol_liq_wash_min": VOL_LIQ_WASH_MIN,
    "hourly_vol_liq_wash_min": HOURLY_VOL_LIQ_WASH_MIN,
    "txns_per_buyer_wash_min": TXNS_PER_BUYER_WASH_MIN,
    "liquidity_floor_usd": LIQUIDITY_FLOOR_USD,
    "age_too_new_hours": AGE_TOO_NEW_HOURS,
    "age_confirmed_hours": AGE_CONFIRMED_HOURS,
}


def lines() -> dict:
    """The lines in force: the defaults with the install's ``screen.pool`` over them."""
    return lines_checked()[0]


def lines_checked():
    """``(lines, error)``; *error* names install lines that could not be applied."""
    from core.trading_presets import screen_lines_checked
    return screen_lines_checked("pool", DEFAULT_LINES)


#: Quote assets that make a pool a TOKENIZED-STOCK pair. Such a position is two bets stacked (the meme and the
#: stock), which is a fact about the position, not a verdict on it.
STOCK_QUOTE_SYMBOLS = frozenset({
    "NVDA", "AAPL", "SPY", "GME", "TSLA", "AMZN", "META", "MSFT", "AMD",
    "INTC", "COIN", "HOOD", "QQQ", "SPCX", "OPENAI", "NFLX", "GOOGL", "PLTR",
})

#: Verdicts, weakest claim to strongest. UNSCREENABLE is not a middle grade —
#: it means the screen did not run.
VERDICTS = ("UNSCREENABLE", "WASH", "TOO_NEW", "PASS", "CANDIDATE", "SURVIVOR")


@dataclass(frozen=True)
class Verdict:
    """One pool's reading. ``reasons`` says why; ``unknowns`` says what was not
    readable, so a thin verdict never passes for a thorough one."""
    verdict: str
    reasons: List[str] = field(default_factory=list)
    unknowns: List[str] = field(default_factory=list)
    stock_pair: bool = False


def is_stock_pair(pool_name: Optional[str]) -> bool:
    """True when the QUOTE side of ``BASE / QUOTE`` is a tokenized stock.

    The quote side only: a token merely NAMED after a stock is a meme about a
    stock, not a position in one, and conflating them would tag half the chain.
    """
    if not pool_name or "/" not in pool_name:
        return False
    quote = pool_name.rsplit("/", 1)[-1].strip().split()[0].upper() if pool_name.rsplit("/", 1)[-1].strip() else ""
    return quote in STOCK_QUOTE_SYMBOLS


def classify(pool) -> Verdict:
    """A verdict for one pool, with its reasons and its blind spots (pure apart
    from reading the install's screen lines)."""
    stock_pair = is_stock_pair(getattr(pool, "name", None))
    L, lines_error = lines_checked()
    if lines_error:
        # The install's lines may be stricter than the defaults: no verdict on
        # lines we could not read.
        return Verdict("UNSCREENABLE", [lines_error + " — no verdict on the default lines"],
                       [lines_error], stock_pair)
    wash, hourly_wash = L["vol_liq_wash_min"], L["hourly_vol_liq_wash_min"]
    per_buyer_wash, floor = L["txns_per_buyer_wash_min"], L["liquidity_floor_usd"]
    engage, too_new, confirmed = (L["vol_liq_engage_max"], L["age_too_new_hours"],
                                  L["age_confirmed_hours"])
    unknowns: List[str] = []
    reasons: List[str] = []

    liquidity = getattr(pool, "liquidity_usd", None)
    volume = getattr(pool, "volume_h24_usd", None)
    vol_liq = getattr(pool, "vol_liq_ratio", None)
    hourly = getattr(pool, "hourly_vol_liq_ratio", None)
    per_buyer = getattr(pool, "txns_per_buyer", None)
    age = getattr(pool, "age_hours", None)

    if liquidity is None:
        unknowns.append("liquidity was not reported by the indexer")
    if volume is None:
        unknowns.append("24h volume was not reported by the indexer")
    if age is None:
        unknowns.append("pool age is unknown (no creation time was reported)")
    if per_buyer is None:
        unknowns.append("unique buyers were not reported, so transactions "
                        "per buyer could not be checked")
    if hourly is None:
        unknowns.append("1h volume was not reported, so a wash in progress "
                        "could not be checked")

    # --- wash first: it is the only call worth making on a partial reading,
    # and the evidence for it does not need the pool's age.
    if vol_liq is not None and vol_liq > wash:
        reasons.append(f"V/L {vol_liq:,.1f} is above {wash:,.0f} — "
                       f"more flow than this depth can explain")
    if hourly is not None and hourly > hourly_wash:
        reasons.append(f"1h volume is {hourly:,.1f}x liquidity — a wash IN PROGRESS, "
                       f"which the 24h figure averages away")
    if per_buyer is not None and per_buyer > per_buyer_wash:
        reasons.append(f"{per_buyer:,.1f} transactions per unique buyer — bot "
                       f"ping-pong")
    if reasons:
        return Verdict("WASH", reasons, unknowns, stock_pair)

    # --- without both sides of the ratio nothing further can be said.
    if vol_liq is None:
        return Verdict("UNSCREENABLE", [
            "the volume-over-liquidity ratio could not be computed, and it is "
            "the strongest separator there is — no verdict is available",
        ], unknowns, stock_pair)

    if age is not None and age < too_new:
        return Verdict("TOO_NEW", [
            f"the pool is {age:,.0f}h old — under {too_new:,.0f}h "
            f"nothing has been proven either way",
        ], unknowns, stock_pair)

    if (liquidity or 0.0) < floor:
        return Verdict("PASS", [
            f"liquidity {liquidity:,.0f} is below the {floor:,.0f} "
            f"floor — shallow, not necessarily manipulated",
        ], unknowns, stock_pair)

    if vol_liq > engage:
        return Verdict("PASS", [
            f"V/L {vol_liq:,.1f} is above the {engage:,.0f} engage "
            f"line but below the wash line — busy, unexplained, not proven",
        ], unknowns, stock_pair)

    healthy = [f"liquidity {liquidity:,.0f}", f"V/L {vol_liq:,.1f}"]
    if per_buyer is not None:
        healthy.append(f"{per_buyer:,.1f} txns per buyer")
    if age is None:
        return Verdict("CANDIDATE", healthy + [
            f"age unknown, so the {confirmed:,.0f}h survival test "
            f"could not be applied",
        ], unknowns, stock_pair)
    if age < confirmed:
        return Verdict("CANDIDATE", healthy + [
            f"{age:,.0f}h old — under {confirmed:,.0f}h it has not "
            f"survived anything yet",
        ], unknowns, stock_pair)
    return Verdict("SURVIVOR", healthy + [f"{age / 24:,.0f}d old"],
                   unknowns, stock_pair)
