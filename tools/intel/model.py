"""Typed facts the read layer returns (071 §3.2).

Every figure is Optional: ``None`` means UNKNOWN, never zero. A source that
failed is NAMED (``SourceAnswer.error``), never silently dropped and never
rendered as a price of 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

#: Confidence values. ``high`` is the ONLY one a spend may size or value with.
HIGH = "high"
LOW = "low"
UNKNOWN = "unknown"
#: A second source answered and disagreed beyond the threshold. Shown, never
#: used to size or value a spend — every ``confidence == "high"`` check already
#: treats it as not-high.
DISPUTED = "disputed"
#: DISPLAY-ONLY label for a low-confidence price that comes from ONE pool. The
#: ``confidence`` field itself never carries it (callers compare that field).
SINGLE_POOL = "single_pool"


@dataclass(frozen=True)
class SourceAnswer:
    """What ONE source said about ``(chain, address)``.

    ``usd=None`` with ``error=None`` = the source answered and has no price
    (no pool). ``error`` set = the source did not answer — a different fact.
    """
    name: str
    usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    priced_liquidity_usd: Optional[float] = None
    pool: Optional[str] = None
    pool_count: Optional[int] = None
    #: The source's own grade (only the primary pool source grades).
    confidence: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class PriceQuote:
    """One agreed price for ``(chain, address)``.

    ``usd`` is the median of the sources that answered. ``primary_usd`` is the
    pool-backed number of the primary source (DexScreener) — the figure the
    spend path has always used, so adding sources can only make it STRICTER
    (a dispute removes the price), never move a cap.

    The ``price_usd`` / ``priced_pool_address`` properties keep the shape of
    ``tools.defi.providers.base.PriceInfo`` so every existing reader works.
    """
    usd: Optional[float]
    sources: Tuple[Tuple[str, float], ...] = ()
    spread_pct: Optional[float] = None
    liquidity_usd: Optional[float] = None
    pool: Optional[str] = None
    pool_count: int = 0
    age_s: float = 0.0
    confidence: str = UNKNOWN
    reason: str = ""
    priced_liquidity_usd: Optional[float] = None
    primary_usd: Optional[float] = None
    primary_confidence: str = UNKNOWN
    primary_liquidity_usd: Optional[float] = None
    failed: Tuple[Tuple[str, str], ...] = ()
    fetched_at: float = field(default=0.0, compare=False)
    #: A gap worth knowing that did NOT change the grade (eased 2026-10-02):
    #: sources a few percent apart, or one outvoted outlier.
    note: Optional[str] = None

    # -- PriceInfo compatibility ------------------------------------------
    @property
    def price_usd(self) -> Optional[float]:
        return self.usd

    @property
    def priced_pool_address(self) -> Optional[str]:
        return self.pool

    # -- readings ---------------------------------------------------------
    @property
    def disputed(self) -> bool:
        return self.confidence == DISPUTED

    @property
    def display_confidence(self) -> str:
        """``single_pool`` for a low grade that is one pool; else ``confidence``.
        For rendering only — never compare a spend decision against this."""
        if self.confidence == LOW and self.usd is not None and self.pool_count == 1:
            return SINGLE_POOL
        return self.confidence

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "usd": self.usd,
            "confidence": self.confidence,
            "display_confidence": self.display_confidence,
            "sources": [{"name": n, "usd": v} for n, v in self.sources],
            "failed_sources": [{"name": n, "error": e} for n, e in self.failed],
            "spread_pct": self.spread_pct,
            "liquidity_usd": self.liquidity_usd,
            "priced_liquidity_usd": self.priced_liquidity_usd,
            "pool": self.pool,
            "pool_count": self.pool_count,
            "age_s": round(self.age_s, 3),
            "primary_usd": self.primary_usd,
            "reason": self.reason,
            "note": self.note,
        }
