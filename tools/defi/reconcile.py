"""Ledger ⟷ chain reconciliation (the §0 fix, 2026-08-26).

On 2026-08-25 the agent's position ledger and the chain disagreed — three
positions held on-chain, "Open positions: NONE" in the ledger's own state table
— and the agent published the wrong side of that disagreement to X. The write
path was correct; the RECALL was broken: runs answered from a stale or partial
read and nothing ever compared the two sources. This module is that comparison.

Pure logic, no I/O: `parse_open_positions` reads the `## Open positions`
markdown table out of the agent-authored ledger, `diff` compares it against a
list of on-chain holdings, `render` produces a verdict the agent is told to
treat as authoritative over its own memory. The network side (balances,
identity, prices) is the `defi_data.reconcile` action's job, through the same
seams `portfolio` uses.

Design constraints that matter:
- UNKNOWN is never zero. A balance the RPC could not read is reported as
  unverifiable, not as "the position is gone" — collapsing the two is exactly
  the failure this module exists to catch (and the one the Solana simulation
  fix a95944b6 caught the same day: code that PASSES while observing nothing).
- Chain holdings that the ledger does not explain are NOT auto-promoted to
  positions — the anti-dust rule stands. They are reported as a disagreement
  the agent must resolve in the ledger, one way or the other.
- The quote asset (USDC) and the wrapped native are working capital, not
  positions; they are never "unexplained".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Confidently-priced holdings below this are classified as dust, not as a
#: disagreement — same spirit as the portfolio verb's dust framing.
DUST_VALUE_USD = 0.25

#: |chain − ledger| / ledger beyond this is a size mismatch. The ledger rounds
#: (442,232.099224 recorded as 442,232.10), so exact equality is the wrong bar.
QTY_REL_TOLERANCE = 0.02

# The LEDGER-READING half is core-tier (2026-08-28): `core/status_snapshot.py`
# reports the open-position COUNT, and core may not import tools. Re-exported
# here so every existing `from tools.defi.reconcile import …` keeps working —
# there is still exactly ONE parser, it just lives one tier down.
from core.position_ledger import (  # noqa: F401  (re-export)
    _BASE58_ADDR, _EVM_ADDR, _norm, _parse_qty, LedgerPosition,
    parse_open_positions,
)


@dataclass
class ChainHolding:
    address: str
    symbol: Optional[str]
    qty: Optional[float]        # None = balance or decimals unknown
    raw_units: Optional[int]    # None = balance read FAILED (unknown, not zero)
    value_usd: Optional[float]  # None = no confident price
    balance_known: bool


@dataclass
class ReconcileReport:
    matched: List[str] = field(default_factory=list)
    mismatched: List[str] = field(default_factory=list)
    unbacked: List[str] = field(default_factory=list)       # ledger row, chain says zero
    unexplained: List[str] = field(default_factory=list)    # chain holds it, ledger silent
    unreadable_rows: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)        # could not verify (read failed)
    dust: List[str] = field(default_factory=list)
    skipped_other_family: List[str] = field(default_factory=list)
    ledger_rows: int = 0
    holdings_considered: int = 0

    @property
    def issues(self) -> int:
        return (len(self.mismatched) + len(self.unbacked)
                + len(self.unexplained) + len(self.unreadable_rows))

    @property
    def verdict(self) -> str:
        if self.issues:
            return "DISAGREEMENT"
        if self.unknown:
            return "UNVERIFIED"
        return "CLEAN"

    def to_dict(self) -> dict:
        """Every list + the scalar fields, plain-dict shaped so the core tier
        (``core.book.verdict_from_report``) can consume it without importing
        this module (core may not import ``tools.*``)."""
        fields = ("matched", "mismatched", "unbacked", "unexplained",
                  "unreadable_rows", "unknown", "dust", "skipped_other_family")
        return {
            **{f: list(getattr(self, f)) for f in fields},
            "ledger_rows": self.ledger_rows,
            "holdings_considered": self.holdings_considered,
            "verdict": self.verdict,
        }


def diff(ledger: List[LedgerPosition],
         holdings: List[ChainHolding],
         *,
         quote_addresses: Optional[List[str]] = None,
         evm_chain: bool = True,
         dust_value_usd: float = DUST_VALUE_USD,
         qty_rel_tolerance: float = QTY_REL_TOLERANCE) -> ReconcileReport:
    report = ReconcileReport()
    quotes = {_norm(a) for a in (quote_addresses or []) if a}
    by_addr: Dict[str, ChainHolding] = {_norm(h.address): h for h in holdings}
    ledger_addrs = set()
    report.ledger_rows = len(ledger)
    report.holdings_considered = len(holdings)

    for row in ledger:
        is_evm_row = bool(_EVM_ADDR.fullmatch(row.address))
        if is_evm_row != evm_chain:
            report.skipped_other_family.append(
                f"{row.symbol} {row.address} — a different chain family; not "
                f"checked here")
            continue
        key = _norm(row.address)
        ledger_addrs.add(key)
        held = by_addr.get(key)
        # Absent from a COMPLETE enumeration means zero. The action guarantees
        # this: under partial scan coverage it adds every ledger address to the
        # scan set, so a ledger row is only ever absent when the indexer
        # enumerated everything and did not see it.
        chain_qty = held.qty if held is not None else 0.0
        if held is not None and not held.balance_known:
            report.unknown.append(
                f"{row.symbol} {row.address} — ledger says {row.qty}, chain "
                f"balance UNKNOWN (read failed — this is NOT zero; retry "
                f"before concluding anything)")
            continue
        if held is not None and held.balance_known and held.qty is None:
            report.unknown.append(
                f"{row.symbol} {row.address} — held on chain "
                f"({held.raw_units} raw units) but decimals unknown, size "
                f"not comparable")
            continue
        if row.qty is None:
            report.unreadable_rows.append(
                f"{row.symbol} {row.address} — the table row's size is "
                f"unparseable; chain says {chain_qty}. Rewrite the row.")
            continue
        if not chain_qty:
            report.unbacked.append(
                f"{row.symbol} {row.address} — ledger says {row.qty:,.6f} "
                f"OPEN, chain balance is ZERO. Either the exit was never "
                f"recorded in the table, or the tokens left the wallet: find "
                f"which, record it, close the row.")
            continue
        rel = abs(chain_qty - row.qty) / row.qty if row.qty else 1.0
        if rel > qty_rel_tolerance:
            report.mismatched.append(
                f"{row.symbol} {row.address} — ledger {row.qty:,.6f} vs chain "
                f"{chain_qty:,.6f} ({rel * 100.0:.1f}% apart). A partial exit "
                f"or entry was not recorded.")
        else:
            report.matched.append(
                f"{row.symbol} {row.address} — {chain_qty:,.6f} (ledger "
                f"{row.qty:,.6f}) ✓")

    for key, held in sorted(by_addr.items()):
        if key in ledger_addrs or key in quotes:
            continue
        label = held.symbol or "?"
        if not held.balance_known:
            report.unknown.append(
                f"{label} {held.address} — balance read FAILED (unknown, "
                f"not zero)")
            continue
        if not held.raw_units and (held.qty is None or held.qty == 0):
            continue  # a zero balance (an emptied token account) is not a holding
        if held.value_usd is not None and held.value_usd < dust_value_usd:
            report.dust.append(
                f"{label} {held.address} — ${held.value_usd:.2f}: dust/airdrop "
                f"noise, never a position, never interact")
            continue
        size = (f"{held.qty:,.6f}" if held.qty is not None
                else f"{held.raw_units} raw units")
        value = (f"≈${held.value_usd:,.2f}" if held.value_usd is not None
                 else "unpriced")
        report.unexplained.append(f"{label} {held.address} — {size} ({value})")
    return report


def render(report: ReconcileReport, *, chain: str, holder: str,
           ledger_path: str, coverage: str) -> str:
    lines = [
        f"LEDGER ⟷ CHAIN RECONCILIATION — chain {chain}, holder {holder}",
        f"ledger: {ledger_path}",
        f"chain coverage: {coverage}",
        f"ledger open rows: {report.ledger_rows} · chain holdings considered: "
        f"{report.holdings_considered}",
        "",
        f"VERDICT: {report.verdict}"
        + (f" — {report.issues} issue(s) between the ledger's Open-positions "
           f"table and the chain" if report.issues else ""),
    ]
    sections = [
        ("LEDGER ROWS THE CHAIN DOES NOT BACK", report.unbacked),
        ("SIZE MISMATCHES", report.mismatched),
        ("HELD ON CHAIN, NOT IN THE LEDGER — each is either a position you "
         "forgot to record (add the row) or something someone sent you "
         "(record it as dust, never interact); decide and write it down NOW",
         report.unexplained),
        ("UNPARSEABLE TABLE ROWS", report.unreadable_rows),
        ("COULD NOT VERIFY (unknown is NOT zero)", report.unknown),
        ("matched", report.matched),
        ("dust (noise, per the anti-dust rule)", report.dust),
        ("other chain family (not checked here)", report.skipped_other_family),
    ]
    for title, items in sections:
        if items:
            lines += ["", f"{title}:"] + [f"  {item}" for item in items]
    lines += [
        "",
        "This output is authoritative over your memory, over the ledger's run",
        "log, and over any earlier summary of the book. If it disagrees with",
        "what you believed, the belief is wrong. Fix the Open-positions table",
        "to match the chain BEFORE any trade, any ledger write, and any public",
        "claim about your positions or track record.",
    ]
    return "\n".join(lines)
