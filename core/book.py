"""The typed reconcile verdict (043 A34) — core tier.

The console's `/positions` page used to derive its verdict by regexing the
`defi_data.reconcile` verb's rendered PROSE (`/DISAGREEMENT/i`, then
`/AGREEMENT|zero disagreements|AGREES/i`). The verb's own closing footer is
always present and reads "If it dis**agrees** with what you believed..." —
that string alone matches the green branch, so a report whose real verdict
was UNVERIFIED (every balance read failed) rendered as green "IN AGREEMENT".
This module replaces prose-sniffing with a typed value derived from the
report's own lists.

Layering: this is core-tier and consumes the plain DICT that
``tools.defi.reconcile.ReconcileReport.to_dict()`` produces — never the
dataclass itself, and never anything from ``tools.*``/``agents.*``
(``tests/test_layering_ratchet.py`` enforces this).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class BookVerdict(str, Enum):
    CLEAN = "clean"
    DISAGREEMENT = "disagreement"
    UNVERIFIED = "unverified"
    NO_LEDGER = "no_ledger"


#: Why the "Entry" (cost basis) / "Since entry" (signed USD P&L) columns read
#: `—` for a position the rail-written store (043 A35, ``core/open_positions.py``)
#: has no cost basis for: the ledger lists the position, but no recorded trade
#: wrote what it cost. A position acquired before A35 landed, or one imported by
#: hand into the ledger, is exactly this case. The surface prints `—` WITH this
#: reason rather than a blank or a fabricated `$0.00`.
NO_ENTRY_RECORDED_REASON = (
    "no entry recorded for this position — the ledger holds it, but no trade "
    "wrote its cost basis")

#: Why "Since entry" reads `—` even when the cost basis IS known: profit/loss is
#: ``worth_now − entry_usd``, and without a current worth there is nothing to
#: subtract from. Distinct from :data:`NO_ENTRY_RECORDED_REASON` so the surface
#: never conflates "no cost basis" with "no current price".
PNL_NEEDS_WORTH_REASON = (
    "worth now is unknown, so profit or loss cannot be computed")


@dataclass
class BookRow:
    symbol: str
    address: str
    chain: Optional[str]
    qty: Optional[float]
    usd: Optional[float]
    state: str  # matched|mismatched|unbacked|unexplained|unknown
    #: Reason ``usd`` (worth now) is None — an unreadable chain, an unpriced
    #: holding — so the surface can print `—` WITH its reason, never `$0.00`.
    worth_now_reason: Optional[str] = None
    #: ``entry`` is the COST BASIS in USD (what the position cost to open, read
    #: from the rail-written store, 043 A35). ``since_entry`` is the SIGNED USD
    #: profit/loss (``worth_now − entry``), NOT a time. Either is None when the
    #: store carries no cost basis (or, for ``since_entry``, when worth_now is
    #: unknown); the paired ``*_reason`` says WHY, so the surface never blanks or
    #: fabricates the value.
    entry: Optional[float] = None
    entry_reason: Optional[str] = None
    since_entry: Optional[float] = None
    since_entry_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        """The JSON row the Money Book renders (043 §3.4): symbol, chain,
        amount, worth_now, entry (USD cost basis), since_entry (signed USD P&L)
        — each paired with a reason so a value no store carries prints `—` with
        its reason, never a confident `$0.00`."""
        return {
            "symbol": self.symbol,
            "address": self.address,
            "chain": self.chain,
            "amount": self.qty,
            "worth_now": self.usd,
            "worth_now_reason": self.worth_now_reason,
            "entry": self.entry,
            "entry_reason": self.entry_reason,
            "since_entry": self.since_entry,
            "since_entry_reason": self.since_entry_reason,
            "state": self.state,
        }


@dataclass
class Book:
    verdict: BookVerdict
    checked_at: float
    chains: List[str]
    coverage: Dict[str, str]
    rows: List[BookRow] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)


def verdict_from_report(d: dict, *, ledger_found: bool) -> BookVerdict:
    """Derive the verdict from a ``ReconcileReport.to_dict()`` shape.

    Never inferred from prose. Order matters: no ledger at all is its own
    state (there was nothing to compare); a genuine disagreement between the
    ledger and the chain outranks an unverifiable balance, which in turn
    outranks a clean book — collapsing UNVERIFIED into CLEAN is exactly the
    failure this module exists to stop.
    """
    if not ledger_found:
        return BookVerdict.NO_LEDGER
    disagreement_keys = ("mismatched", "unbacked", "unexplained", "unreadable_rows")
    if any(d.get(k) for k in disagreement_keys):
        return BookVerdict.DISAGREEMENT
    if d.get("unknown"):
        return BookVerdict.UNVERIFIED
    return BookVerdict.CLEAN
