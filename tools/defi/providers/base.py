"""Provider seam for third-party token claims.

Everything a provider returns is a CLAIM, not a fact: prices come from pools
anyone can seed, and a screener is a heuristic over known patterns. Providers
live in the tools tier precisely because `core/wallet/` must hold only what the
chain enforces (and cannot import tools — 5-tier ratchet).

Failure semantics differ by kind, deliberately:

  * **data** (price, liquidity) fails OPEN — a missing price renders "unknown",
    never "$0".
  * **screening** fails CLOSED — an unreachable screener renders "unavailable",
    never "safe". In this read-only tier that is an honest label; at proposal-023
    T3 it is the field that routes a trade to owner_queue, so it must never
    overstate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

#: Below this, a pool is cheap enough for an attacker to seed, so any price it
#: implies is not trustworthy for valuation. Mirrors proposal 023's
#: DEFI_TOKEN_SCREEN_MIN_LIQUIDITY_USD default.
LIQUIDITY_CONFIDENCE_FLOOR_USD = 50_000.0


@dataclass(frozen=True)
class Candidate:
    """One contract that claims a given ticker. Never 'the' token for a symbol."""
    chain: str
    address: str
    symbol: Optional[str]
    name: Optional[str]
    liquidity_usd: float
    price_usd: Optional[float]


@dataclass(frozen=True)
class PriceInfo:
    """Price with an explicit trust signal.

    ``confidence`` is "high" only for a token priced across multiple pools with
    real depth. "low" means the price is attacker-influenceable and must not be
    summed into a headline total. "unknown" means there is no price at all.
    """
    price_usd: Optional[float]
    liquidity_usd: Optional[float]
    pool_count: int
    confidence: str  # "high" | "low" | "unknown"


@dataclass(frozen=True)
class ScreenVerdict:
    """A screening result that enumerates its checks.

    There is deliberately no boolean ``safe``: "passed" reads as "safe", and a
    check that did not run is not a check that passed. ``available=False`` means
    the screener said nothing — which is not a clean bill of health.
    """
    available: bool
    checks: Dict[str, str] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)


@runtime_checkable
class Provider(Protocol):
    name: str

    def health(self) -> bool:
        ...


def confidence_for(price_usd: Optional[float], liquidity_usd: Optional[float],
                   pool_count: int) -> str:
    if price_usd is None:
        return "unknown"
    if (liquidity_usd or 0.0) >= LIQUIDITY_CONFIDENCE_FLOOR_USD and pool_count >= 2:
        return "high"
    return "low"
