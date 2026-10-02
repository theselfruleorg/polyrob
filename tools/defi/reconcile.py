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

071 W3 (TM P0-7, P1-13):
- TWO books are diffed against the chain: the RAIL store (``open_positions``,
  written from landed receipts) and the agent's markdown ledger. Every row says
  which source it came from.
- An UNPRICED token neither book mentions is ``unsolicited`` (someone sent it;
  never interact) — NOT a disagreement, so a dusted wallet can read CLEAN. A
  priced holding above dust that no book explains stays ``unexplained``.
- A ledger row closed to 0 whose chain balance is 0 is a matched CLOSED row,
  never "unbacked".
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
    #: The contract's self-reported name (068). Shown beside the address when two
    #: holdings claim one symbol — it is what tells them apart for a reader.
    name: Optional[str] = None
    #: 071 review: what the PRICE read said, so "unpriced" is not one bucket.
    #: ``high`` | ``priced`` (low/disputed — a market exists) | ``no_pool`` (the
    #: sources answered: nothing trades it) | ``failed`` (a source did not
    #: answer). ``None`` = not recorded (older callers) — read as ``no_pool``.
    price_state: Optional[str] = None
    #: Value at ANY confidence (low/disputed): used only to tell dust from a
    #: real unrecorded holding, never as a money figure.
    est_value_usd: Optional[float] = None


@dataclass
class RailPosition:
    """071 W3: one row of the rail-written store, as reconcile needs it."""
    address: str
    symbol: Optional[str]
    qty: float
    #: ``open`` | ``quarantined`` | ``written_off``. A written-off row explains a
    #: holding but is not compared by size (the owner's loss verdict).
    status: str = "open"


@dataclass
class ReconcileReport:
    matched: List[str] = field(default_factory=list)
    mismatched: List[str] = field(default_factory=list)
    unbacked: List[str] = field(default_factory=list)       # ledger row, chain says zero
    unexplained: List[str] = field(default_factory=list)    # chain holds it, ledger silent
    #: 071 W3: unpriced, in neither book — someone sent it. Not an issue.
    unsolicited: List[str] = field(default_factory=list)
    unreadable_rows: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)        # could not verify (read failed)
    dust: List[str] = field(default_factory=list)
    skipped_other_family: List[str] = field(default_factory=list)
    #: 068: warning lines when >1 contract here claims one symbol. Rendered
    #: FIRST — on 2026-09-25 a buyback took the first "PNL" line it saw.
    collisions: List[str] = field(default_factory=list)
    ledger_rows: int = 0
    holdings_considered: int = 0
    #: 071 W3: rail-store rows compared; None = the rail store was not consulted.
    rail_rows: Optional[int] = None
    #: CR-L11: the ADDRESS each row is about, per list, parallel to the text.
    #: A row's text starts with a symbol, and a symbol is attacker-authored (a
    #: contract's ``symbol()`` or a ledger cell), so "the first address in the
    #: line" could be an address the symbol smuggled in. Consumers
    #: (``tools/defi/book.py``) match on THIS field, never on the prose.
    addresses: Dict[str, List[str]] = field(default_factory=dict)

    def add(self, kind: str, address: str, text: str) -> None:
        getattr(self, kind).append(text)
        self.addresses.setdefault(kind, []).append(address)

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
                  "unsolicited", "unreadable_rows", "unknown", "dust",
                  "skipped_other_family", "collisions")
        return {
            **{f: list(getattr(self, f)) for f in fields},
            "addresses": {k: list(v) for k, v in self.addresses.items()},
            "ledger_rows": self.ledger_rows,
            "rail_rows": self.rail_rows,
            "holdings_considered": self.holdings_considered,
            "verdict": self.verdict,
        }


def diff(ledger: List[LedgerPosition],
         holdings: List[ChainHolding],
         *,
         quote_addresses: Optional[List[str]] = None,
         evm_chain: bool = True,
         dust_value_usd: float = DUST_VALUE_USD,
         qty_rel_tolerance: float = QTY_REL_TOLERANCE,
         rail: Optional[List[RailPosition]] = None) -> ReconcileReport:
    """Diff the chain against the rail store (``rail``; None = not consulted)
    and the markdown ledger. See the module docstring for the classes."""
    from core.wallet.tokens import clean_symbol
    report = ReconcileReport()
    quotes = {_norm(a) for a in (quote_addresses or []) if a}
    by_addr: Dict[str, ChainHolding] = {_norm(h.address): h for h in holdings}
    ledger_addrs = set()
    report.ledger_rows = len(ledger)
    report.holdings_considered = len(holdings)

    for row in ledger:
        is_evm_row = bool(_EVM_ADDR.fullmatch(row.address))
        if is_evm_row != evm_chain:
            report.add("skipped_other_family", row.address,
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
            report.add("unknown", row.address,
                f"{row.symbol} {row.address} — ledger says {row.qty}, chain "
                f"balance UNKNOWN (read failed — this is NOT zero; retry "
                f"before concluding anything)")
            continue
        if held is not None and held.balance_known and held.qty is None:
            report.add("unknown", row.address,
                f"{row.symbol} {row.address} — held on chain "
                f"({held.raw_units} raw units) but decimals unknown, size "
                f"not comparable")
            continue
        if row.qty is None:
            report.add("unreadable_rows", row.address,
                f"{row.symbol} {row.address} — the table row's size is "
                f"unparseable; chain says {chain_qty}. Rewrite the row.")
            continue
        if not chain_qty and not row.qty:
            # TM P1-13: the ledger closed it (size 0) and the chain agrees.
            report.add("matched", row.address,
                f"{row.symbol} {row.address} — closed (ledger 0, chain 0) ✓ "
                f"[ledger]")
            continue
        if not chain_qty:
            report.add("unbacked", row.address,
                f"{row.symbol} {row.address} — ledger says {row.qty:,.6f} "
                f"OPEN, chain balance is ZERO. Either the exit was never "
                f"recorded in the table, or the tokens left the wallet: find "
                f"which, record it, close the row. [ledger]")
            continue
        rel = abs(chain_qty - row.qty) / row.qty if row.qty else 1.0
        if rel > qty_rel_tolerance:
            report.add("mismatched", row.address,
                f"{row.symbol} {row.address} — ledger {row.qty:,.6f} vs chain "
                f"{chain_qty:,.6f} ({rel * 100.0:.1f}% apart). A partial exit "
                f"or entry was not recorded. [ledger]")
        else:
            report.add("matched", row.address,
                f"{row.symbol} {row.address} — {chain_qty:,.6f} (ledger "
                f"{row.qty:,.6f}) ✓ [ledger]")

    rail_addrs = _diff_rail(report, rail, by_addr, ledger_addrs,
                            evm_chain=evm_chain, qty_rel_tolerance=qty_rel_tolerance)

    # 068: one symbol, several contracts — across the ledger rows AND the chain
    # holdings, named, before anything else is read.
    names = {_norm(h.address): getattr(h, "name", None) for h in holdings}
    # 068 B11: the quote assets take part — a fake "USDC" beside the real one
    # is exactly the collision to name. Only their POSITION treatment differs.
    collision_rows = [(clean_symbol(h.symbol), h.address, names.get(_norm(h.address)))
                      for h in holdings]
    held_keys = {_norm(h.address) for h in holdings}
    collision_rows += [(clean_symbol(r.symbol), r.address, "ledger row")
                       for r in ledger if _norm(r.address) not in held_keys]
    # A pinned quote asset the wallet does not hold right now still OWNS its
    # symbol: a look-alike claiming it must warn even with no real one held.
    try:
        from core.wallet.tokens import CANONICAL_TOKENS
        canon = {_norm(a): meta for (_c, a), meta in CANONICAL_TOKENS.items()}
    except Exception:
        canon = {}
    for q in sorted(quotes - held_keys):
        meta = canon.get(q)
        if meta:
            collision_rows.append((str(meta.get("symbol")), q, "canonical pin"))
    report.collisions = [ln for ln in _ticker_collision_lines(collision_rows) if ln]

    for key, held in sorted(by_addr.items()):
        if key in ledger_addrs or key in quotes or key in rail_addrs:
            continue
        label = clean_symbol(held.symbol) or "?"
        if not held.balance_known:
            report.add("unknown", held.address,
                f"{label} {held.address} — balance read FAILED (unknown, "
                f"not zero)")
            continue
        if not held.raw_units and (held.qty is None or held.qty == 0):
            continue  # a zero balance (an emptied token account) is not a holding
        if held.value_usd is not None and held.value_usd < dust_value_usd:
            report.add("dust", held.address,
                f"{label} {held.address} — ${held.value_usd:.2f}: dust/airdrop "
                f"noise, never a position, never interact")
            continue
        size = (f"{held.qty:,.6f}" if held.qty is not None
                else f"{held.raw_units} raw units")
        if held.value_usd is None:
            state = held.price_state or "no_pool"
            est = held.est_value_usd
            if state == "failed":
                # 071 review: an outage must not turn every unrecorded holding
                # into "clean" — we cannot tell an airdrop from a position.
                report.add("unknown", held.address,
                    f"{label} {held.address} — {size} (price read failed; in "
                    f"neither book — cannot tell an airdrop from a position)")
                continue
            if est is not None and est < dust_value_usd:
                report.add("dust", held.address,
                    f"{label} {held.address} — ≈${est:.2f} at a low-confidence "
                    f"price: dust/airdrop noise, never a position, never interact")
                continue
            if state == "no_pool":
                # TM P0-7: a token nothing trades, that no book mentions, is
                # something someone SENT — an airdrop or a scam lure. Not a
                # disagreement, or a dusted wallet never reads CLEAN.
                report.add("unsolicited", held.address,
                    f"{label} {held.address} — {size} (no market; in neither book)")
                continue
            # A market exists (low/disputed price) and it is worth more than
            # dust: an unrecorded buy looks exactly like this. A disagreement.
            report.add("unexplained", held.address,
                f"{label} {held.address} — {size} ("
                + (f"≈${est:,.2f} at a low-confidence price" if est is not None
                   else "priced at low confidence") + "; in neither book)")
            continue
        report.add("unexplained", held.address,
            f"{label} {held.address} — {size} (≈${held.value_usd:,.2f}; in "
            f"neither book)")
    return report


def _diff_rail(report: ReconcileReport, rail: Optional[List[RailPosition]],
               by_addr: Dict[str, ChainHolding], ledger_addrs: set, *,
               evm_chain: bool, qty_rel_tolerance: float) -> set:
    """071 W3: the RAIL store against the chain. Returns the addresses the rail
    store explains. A row the ledger also lists only adds a line when the rail
    store DISAGREES with the chain (the ledger line already says the rest)."""
    from core.wallet.tokens import clean_symbol
    explained: set = set()
    if rail is None:
        return explained
    report.rail_rows = 0
    for rp in rail:
        if bool(_EVM_ADDR.fullmatch(rp.address or "")) != evm_chain:
            continue
        report.rail_rows += 1
        key = _norm(rp.address)
        explained.add(key)
        label = clean_symbol(rp.symbol) or "?"
        in_ledger = key in ledger_addrs
        held = by_addr.get(key)
        if rp.status != "open":
            if not in_ledger:
                report.add("matched", rp.address,
                    f"{label} {rp.address} — the rail store marks it "
                    f"{rp.status.replace('_', ' ')}; not compared [rail store]")
            continue
        if held is not None and (not held.balance_known or held.qty is None):
            if not in_ledger:
                report.add("unknown", rp.address,
                    f"{label} {rp.address} — rail store says {rp.qty:,.6f}, chain "
                    f"balance UNKNOWN (unknown is NOT zero) [rail store]")
            continue
        chain_qty = held.qty if held is not None else 0.0
        if not chain_qty:
            report.add("unbacked", rp.address,
                f"{label} {rp.address} — rail store says {rp.qty:,.6f} OPEN, "
                f"chain balance is ZERO. The tokens left the wallet outside a "
                f"recorded swap (a transfer?), or an exit was not booked. "
                f"[rail store]")
            continue
        rel = abs(chain_qty - rp.qty) / rp.qty if rp.qty else 1.0
        if rel > qty_rel_tolerance:
            report.add("mismatched", rp.address,
                f"{label} {rp.address} — rail store {rp.qty:,.6f} vs chain "
                f"{chain_qty:,.6f} ({rel * 100.0:.1f}% apart). Tokens moved "
                f"outside a recorded swap. [rail store]")
            continue
        if not in_ledger:
            report.add("unexplained", rp.address,
                f"{label} {rp.address} — {chain_qty:,.6f} held; the rail store "
                f"agrees ({rp.qty:,.6f}) but the ledger has NO row. Add the row. "
                f"[rail store, not in ledger]")
    return explained


def render(report: ReconcileReport, *, chain: str, holder: str,
           ledger_path: str, coverage: str,
           rail_state: Optional[str] = None) -> str:
    lines = [
        f"LEDGER ⟷ CHAIN RECONCILIATION — chain {chain}, holder {holder}",
        f"ledger: {ledger_path}",
        "rail store (open_positions): "
        + (rail_state or ("not consulted" if report.rail_rows is None
                          else f"{report.rail_rows} row(s) compared")),
        f"chain coverage: {coverage}",
        f"ledger open rows: {report.ledger_rows} · chain holdings considered: "
        f"{report.holdings_considered}",
        "Each row ends with its source: [ledger] = your markdown table, "
        "[rail store] = what the money rail recorded from landed receipts.",
        "",
        f"VERDICT: {report.verdict}"
        + (f" — {report.issues} issue(s) between the chain and the books "
           f"(the ledger's Open-positions table and the rail store)"
           if report.issues else ""),
    ]
    if report.collisions:
        lines += [""] + list(report.collisions)
    sections = [
        ("matched", report.matched),
        ("LEDGER ROWS THE CHAIN DOES NOT BACK", report.unbacked),
        ("SIZE MISMATCHES", report.mismatched),
        ("HELD ON CHAIN, NOT IN THE LEDGER — each is either a position you "
         "forgot to record (add the row) or something someone sent you "
         "(record it as dust, never interact); decide and write it down NOW",
         report.unexplained),
        ("unsolicited (unpriced, in neither book — someone sent it; never "
         "interact; not an issue. If you DID buy one, add its ledger row)",
         report.unsolicited),
        ("UNPARSEABLE TABLE ROWS", report.unreadable_rows),
        ("COULD NOT VERIFY (unknown is NOT zero)", report.unknown),
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


def _ticker_collision_lines(rows) -> list:
    """Warning lines when this wallet holds >1 contract claiming one symbol.

    ⚠️ On 2026-09-23 the SAFETY rail picked the token it was monitoring out of
    the portfolio BY SYMBOL: *"PNL token address is 0x357A0436… — that is the
    PNL contract."* It was not. The treasury holds that ticker twice — the
    99,090,018-token buyback position and 615 tokens of a different contract
    received as dust — and every measurement after that premise was a correct
    reading of the wrong token, reported to the owner as a 78% liquidity
    collapse that never happened.

    Nothing downstream could catch it: `token_info` names the contract it was
    ASKED about, and a price names the pool it came from, but the address was
    chosen before either ran. So the warning belongs here, on the screen where
    the choice is made.

    ``rows`` is ``(symbol, address)`` or ``(symbol, address, name)`` per
    holding. The NAME is shown beside each address because it is what actually
    separates them: read live on 2026-09-23, both contracts report symbol
    ``PNL`` and their names are ``Rob Track Record`` and ``Pissin N Lying``.
    The symbol cannot tell them apart and an address is 42 characters of hex.
    Showing the name is not ranking them — it hands over the other identifier
    the contract itself publishes, and lets the reader recognise their own
    token.

    The check is deliberately
    local — it compares only what this wallet holds and asks no provider which
    ticker is "legitimate", because that question has no honest answer: balance,
    liquidity and volume are all purchasable, so a seeded look-alike can outrank
    a real token on every ranking there is. It therefore names the contracts and
    refuses to pick a winner; the caller must resolve the address from its own
    records.

    An unreadable symbol (``None``/``""``/``"?"``) is never collided with
    another unreadable one — that would manufacture a warning out of missing
    data.
    """
    groups = {}
    for row in rows:
        symbol, address = row[0], row[1]
        name = row[2] if len(row) > 2 else None
        key = (symbol or "").strip().lower()
        if not key or key == "?":
            continue
        # 068 B3: EVM hex folds case; base58 does NOT (two Solana mints that
        # differ only in case are two accounts).
        groups.setdefault(key, {})[_norm(address or "")] = (address, name)
    out = []
    for key in sorted(groups):
        seen = groups[key]
        if len(seen) < 2:
            continue
        if not out:
            out += ["", "⚠ ONE SYMBOL, MORE THAN ONE CONTRACT — in this wallet:",
                    "  Do NOT resolve these by symbol. Which contract you mean is "
                    "not something this list can tell you and not something any "
                    "price or liquidity ranking can settle — a look-alike can be "
                    "seeded to outrank a real token. Take the address from your "
                    "own ledger row or the owner's instruction, and say which "
                    "address you used.", ""]
        entries = [seen[k] for k in sorted(seen)]
        out.append(f"  symbol {key.upper()!r} is claimed by {len(entries)} contracts here:")
        for address, name in entries:
            label = str(name).strip() if name and str(name).strip() else "name unknown"
            out.append(f"      {address}   {label}")
    return out
