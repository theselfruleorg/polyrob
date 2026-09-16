"""Composite pool verdict, from the thresholds that were actually measured.

Prod built this as a standalone script inside a session workspace
(`tools/rh-scanner.py`) from a study of 5 survivors against 7 wash cases on
Robinhood chain, ran it live across three chains, and then left it outside the
repo, outside deploy and outside CI. This is that screen, promoted: pure, tested,
and reachable as a verb.

**The thresholds are measurements, not opinions** (2026-09-13, chain 4663):

    signal                     winners        wash/dead      line drawn here
    ------------------------   ------------   ------------   ------------------
    24h volume / liquidity     0.06 - 2.1     13 - 124       engage <= 5, wash > 10
    main-pool liquidity        $1.4M - $26.7M $3.7k - $258k  floor $500k
    pool age at decision       44 - 74 days   < 48 hours     >= 48h to confirm
    txns per unique buyer      1.6 - 13       25 - 32        wash > 20
    1h volume / liquidity      <= 1.5x        5x - 40x       wash > 5

Volume-over-liquidity is the strongest single separator: no measured winner
exceeded 2.1 and no measured wash case sat below 13, a full order of magnitude
of clear air. None of them is sufficient alone — CHUMP, a survivor, carried 13
transactions per buyer and a ratio of 2.1.

**A verdict is never built on a missing number.** The workspace script computed
`vl = inf` when liquidity was zero or unknown and called the pool WASH, so an
indexer that had not caught up read exactly like a manipulator. Here an
unreadable input yields UNSCREENABLE with the missing fields named. This screen
also decides nothing about a CONTRACT: honeypot, taxes, mint authority and
holder concentration are `token_info` and `token_holders`, and a SURVIVOR
verdict here is not a safety claim.
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
#: Transactions per UNIQUE 24h buyer. Bot ping-pong measured 25-32.
TXNS_PER_BUYER_WASH_MIN = 20.0
#: Below this depth, a $3 ticket cannot be exited without eating the book.
LIQUIDITY_FLOOR_USD = 500_000.0
#: Under this age nothing has been proven either way.
AGE_TOO_NEW_HOURS = 12.0
#: At or over this age a pool that still looks healthy has survived something.
AGE_CONFIRMED_HOURS = 48.0

#: Quote assets that make a pool a TOKENIZED-STOCK pair — the Robinhood-chain
#: native narrative. Such a position is two bets stacked (the meme and the
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
    """Pure. A verdict for one pool, with its reasons and its blind spots."""
    stock_pair = is_stock_pair(getattr(pool, "name", None))
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
    if vol_liq is not None and vol_liq > VOL_LIQ_WASH_MIN:
        reasons.append(f"V/L {vol_liq:,.1f} is above {VOL_LIQ_WASH_MIN:,.0f} — "
                       f"more flow than this depth can explain")
    if hourly is not None and hourly > HOURLY_VOL_LIQ_WASH_MIN:
        reasons.append(f"1h volume is {hourly:,.1f}x liquidity — a wash IN PROGRESS, "
                       f"which the 24h figure averages away")
    if per_buyer is not None and per_buyer > TXNS_PER_BUYER_WASH_MIN:
        reasons.append(f"{per_buyer:,.1f} transactions per unique buyer — bot "
                       f"ping-pong; measured survivors stayed under 15")
    if reasons:
        return Verdict("WASH", reasons, unknowns, stock_pair)

    # --- without both sides of the ratio nothing further can be said.
    if vol_liq is None:
        return Verdict("UNSCREENABLE", [
            "the volume-over-liquidity ratio could not be computed, and it is "
            "the strongest separator there is — no verdict is available",
        ], unknowns, stock_pair)

    if age is not None and age < AGE_TOO_NEW_HOURS:
        return Verdict("TOO_NEW", [
            f"the pool is {age:,.0f}h old — under {AGE_TOO_NEW_HOURS:,.0f}h "
            f"nothing has been proven either way",
        ], unknowns, stock_pair)

    if (liquidity or 0.0) < LIQUIDITY_FLOOR_USD:
        return Verdict("PASS", [
            f"liquidity {liquidity:,.0f} is below the {LIQUIDITY_FLOOR_USD:,.0f} "
            f"floor — shallow, not necessarily manipulated",
        ], unknowns, stock_pair)

    if vol_liq > VOL_LIQ_ENGAGE_MAX:
        return Verdict("PASS", [
            f"V/L {vol_liq:,.1f} is above the {VOL_LIQ_ENGAGE_MAX:,.0f} engage "
            f"line but below the wash line — busy, unexplained, not proven",
        ], unknowns, stock_pair)

    healthy = [f"liquidity {liquidity:,.0f}", f"V/L {vol_liq:,.1f}"]
    if per_buyer is not None:
        healthy.append(f"{per_buyer:,.1f} txns per buyer")
    if age is None:
        return Verdict("CANDIDATE", healthy + [
            f"age unknown, so the {AGE_CONFIRMED_HOURS:,.0f}h survival test "
            f"could not be applied",
        ], unknowns, stock_pair)
    if age < AGE_CONFIRMED_HOURS:
        return Verdict("CANDIDATE", healthy + [
            f"{age:,.0f}h old — under {AGE_CONFIRMED_HOURS:,.0f}h it has not "
            f"survived anything yet",
        ], unknowns, stock_pair)
    return Verdict("SURVIVOR", healthy + [f"{age / 24:,.0f}d old"],
                   unknowns, stock_pair)
