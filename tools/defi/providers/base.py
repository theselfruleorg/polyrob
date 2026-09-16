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
    #: Checks this screener knows about that did NOT run on this token/chain.
    #: A screener can answer a chain PARTIALLY (GoPlus covers Robinhood 4663 but
    #: sends no is_honeypot, no holders and empty taxes), and rendering that as
    #: "no risk flags raised" makes a partial screen read exactly like a clean
    #: one. Naming the absent checks is the difference.
    missing: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class HolderRow:
    """One holder line as the screener reports it.

    ``percent`` is a FRACTION (0.30 = 30%), which is how GoPlus sends it. Every
    field is Optional because a missing figure must render "unknown", never 0 —
    "this wallet holds 0%" and "the screener did not say" are different facts.
    """
    address: str
    percent: Optional[float] = None
    is_contract: Optional[bool] = None
    is_locked: Optional[bool] = None
    tag: Optional[str] = None


@dataclass(frozen=True)
class HolderReport:
    """Who owns a token, and whether they can pull the floor out.

    This is the concentration answer — the thing a holder-cluster map is read
    for. It fails CLOSED like a screen, not open like a price: ``available=False``
    with a stated ``reason`` means nobody told us, which is NOT "well
    distributed". An empty ``top_holders`` on an available report would read as
    "nobody holds this", so the two states are kept distinct.

    ``top_percent`` is the sum of the reported top holders INCLUDING contracts
    (a pool contract is usually the largest single line). ``top_percent_wallets``
    excludes rows the screener marked as contracts, because a locked pool is not
    a person who can sell.
    """
    available: bool
    reason: Optional[str] = None
    holder_count: Optional[int] = None
    total_supply: Optional[float] = None
    top_holders: List[HolderRow] = field(default_factory=list)
    lp_holders: List[HolderRow] = field(default_factory=list)
    lp_holder_count: Optional[int] = None
    lp_total_supply: Optional[float] = None
    creator_address: Optional[str] = None
    creator_percent: Optional[float] = None
    owner_address: Optional[str] = None
    owner_percent: Optional[float] = None
    honeypot_with_same_creator: Optional[bool] = None

    @property
    def top_percent(self) -> Optional[float]:
        known = [h.percent for h in self.top_holders if h.percent is not None]
        return sum(known) if known else None

    @property
    def top_percent_wallets(self) -> Optional[float]:
        known = [h.percent for h in self.top_holders
                 if h.percent is not None and h.is_contract is not True]
        return sum(known) if known else None

    @property
    def lp_locked_percent(self) -> Optional[float]:
        """Share of the LP supply held in rows the screener marks as locked or
        burned. Unknown when no LP holder row carries a percent."""
        known = [h.percent for h in self.lp_holders if h.percent is not None]
        if not known:
            return None
        return sum(h.percent for h in self.lp_holders
                   if h.percent is not None and h.is_locked is True)


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
