"""Book ⟷ chain reconciliation (the §0 fix, 2026-08-26).

On 2026-08-25 the agent's recorded positions and the chain disagreed, and the
agent published the wrong side of that disagreement to X. This module is the
comparison: the rail store (``core/open_positions.py``, written from landed
receipts) against the chain.

Pure logic, no I/O: `diff` compares the rail book against a list of on-chain
holdings, `render` produces a verdict the agent is told to treat as
authoritative over its own memory. The network side (balances, identity,
prices) is the `defi_data.reconcile` action's job, through the same seams
`portfolio` uses.

Design constraints that matter:
- UNKNOWN is never zero. A balance the RPC could not read is reported as
  unverifiable, not as "the position is gone".
- Chain holdings that the book does not explain are NOT auto-promoted to
  positions — the anti-dust rule stands.
- The quote asset (USDC) and the wrapped native are working capital, not
  positions; they are never "unexplained".
- An UNPRICED token the book does not hold is ``unsolicited`` (someone sent it;
  never interact) — NOT a disagreement, so a dusted wallet can read CLEAN. A
  priced holding above dust that the book does not explain stays
  ``unexplained``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Confidently-priced holdings below this are classified as dust, not as a
#: disagreement — same spirit as the portfolio verb's dust framing.
DUST_VALUE_USD = 0.25

#: |chain − book| / book beyond this is a size mismatch (a quoted or simulated
#: size is not the final fill), so exact equality is the wrong bar.
QTY_REL_TOLERANCE = 0.02

_EVM_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_BASE58_ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

from core.open_positions import _norm  # noqa: E402


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
    unbacked: List[str] = field(default_factory=list)       # book row, chain says zero
    unexplained: List[str] = field(default_factory=list)    # chain holds it, book silent
    #: unpriced, not in the book — someone sent it. Not an issue.
    unsolicited: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)        # could not verify (read failed)
    dust: List[str] = field(default_factory=list)
    #: 068: warning lines when >1 contract here claims one symbol. Rendered
    #: FIRST — on 2026-09-25 a buyback took the first "PNL" line it saw.
    collisions: List[str] = field(default_factory=list)
    holdings_considered: int = 0
    #: rail-store rows compared on this chain.
    rail_rows: int = 0
    #: CR-L11: the ADDRESS each row is about, per list, parallel to the text.
    #: A row's text starts with a symbol, and a symbol is attacker-authored (a
    #: contract's ``symbol()``), so "the first address in the line" could be an
    #: address the symbol smuggled in. Consumers (``tools/defi/book.py``) match
    #: on THIS field, never on the prose.
    addresses: Dict[str, List[str]] = field(default_factory=dict)

    def add(self, kind: str, address: str, text: str) -> None:
        getattr(self, kind).append(text)
        self.addresses.setdefault(kind, []).append(address)

    @property
    def issues(self) -> int:
        return len(self.mismatched) + len(self.unbacked) + len(self.unexplained)

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
                  "unsolicited", "unknown", "dust", "collisions")
        return {
            **{f: list(getattr(self, f)) for f in fields},
            "addresses": {k: list(v) for k, v in self.addresses.items()},
            "rail_rows": self.rail_rows,
            "holdings_considered": self.holdings_considered,
            "verdict": self.verdict,
        }


def diff(rail: List[RailPosition],
         holdings: List[ChainHolding],
         *,
         quote_addresses: Optional[List[str]] = None,
         evm_chain: bool = True,
         dust_value_usd: float = DUST_VALUE_USD,
         qty_rel_tolerance: float = QTY_REL_TOLERANCE,
         chain: Optional[str] = None) -> ReconcileReport:
    """Diff the chain against the rail book (``rail``: this chain's rows).
    See the module docstring for the classes."""
    from core.wallet.tokens import clean_symbol
    report = ReconcileReport()
    quotes = {_norm(a) for a in (quote_addresses or []) if a}
    by_addr: Dict[str, ChainHolding] = {_norm(h.address): h for h in holdings}
    report.holdings_considered = len(holdings)
    book_addrs = set()

    for rp in rail or []:
        if bool(_EVM_ADDR.fullmatch(rp.address or "")) != evm_chain:
            continue
        report.rail_rows += 1
        key = _norm(rp.address)
        book_addrs.add(key)
        label = clean_symbol(rp.symbol) or "?"
        held = by_addr.get(key)
        if rp.status != "open":
            report.add("matched", rp.address,
                f"{label} {rp.address} — the book marks it "
                f"{rp.status.replace('_', ' ')}; not compared")
            continue
        if held is not None and not held.balance_known:
            report.add("unknown", rp.address,
                f"{label} {rp.address} — book says {rp.qty:,.6f}, chain balance "
                f"UNKNOWN (read failed — this is NOT zero; retry before "
                f"concluding anything)")
            continue
        if held is not None and held.qty is None:
            report.add("unknown", rp.address,
                f"{label} {rp.address} — held on chain ({held.raw_units} raw "
                f"units) but decimals unknown, size not comparable")
            continue
        # Absent from a COMPLETE enumeration means zero. The action guarantees
        # this: under partial scan coverage it adds every book address to the
        # scan set, so a book row is only ever absent when the indexer
        # enumerated everything and did not see it.
        chain_qty = held.qty if held is not None else 0.0
        if not chain_qty:
            report.add("unbacked", rp.address,
                f"{label} {rp.address} — book says {rp.qty:,.6f} OPEN, chain "
                f"balance is ZERO. The tokens left the wallet outside a recorded "
                f"swap (a transfer?), or an exit was not booked. "
                + (f"If it is gone for good, the owner closes the row with "
                   f"/writeoff {chain} {rp.address} (the loss is its recorded "
                   f"cost; nothing is sold)." if chain else ""))
            continue
        rel = abs(chain_qty - rp.qty) / rp.qty if rp.qty else 1.0
        if rel > qty_rel_tolerance:
            report.add("mismatched", rp.address,
                f"{label} {rp.address} — book {rp.qty:,.6f} vs chain "
                f"{chain_qty:,.6f} ({rel * 100.0:.1f}% apart). Tokens moved "
                f"outside a recorded swap.")
        else:
            report.add("matched", rp.address,
                f"{label} {rp.address} — {chain_qty:,.6f} (book {rp.qty:,.6f}) ✓")

    # 068: one symbol, several contracts — across the book rows AND the chain
    # holdings, named, before anything else is read.
    names = {_norm(h.address): getattr(h, "name", None) for h in holdings}
    # 068 B11: the quote assets take part — a fake "USDC" beside the real one
    # is exactly the collision to name. Only their POSITION treatment differs.
    collision_rows = [(clean_symbol(h.symbol), h.address, names.get(_norm(h.address)))
                      for h in holdings]
    held_keys = {_norm(h.address) for h in holdings}
    collision_rows += [(clean_symbol(r.symbol), r.address, "book row")
                       for r in rail or [] if _norm(r.address) not in held_keys
                       and bool(_EVM_ADDR.fullmatch(r.address or "")) == evm_chain]
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
        if key in book_addrs or key in quotes:
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
                    f"{label} {held.address} — {size} (price read failed; not in "
                    f"the book — cannot tell an airdrop from a position)")
                continue
            if est is not None and est < dust_value_usd:
                report.add("dust", held.address,
                    f"{label} {held.address} — ≈${est:.2f} at a low-confidence "
                    f"price: dust/airdrop noise, never a position, never interact")
                continue
            if state == "no_pool":
                # TM P0-7: a token nothing trades, that the book does not hold,
                # is something someone SENT — an airdrop or a scam lure. Not a
                # disagreement, or a dusted wallet never reads CLEAN.
                report.add("unsolicited", held.address,
                    f"{label} {held.address} — {size} (no market; not in the book)")
                continue
            # A market exists (low/disputed price) and it is worth more than
            # dust: an unrecorded buy looks exactly like this. A disagreement.
            report.add("unexplained", held.address,
                f"{label} {held.address} — {size} ("
                + (f"≈${est:,.2f} at a low-confidence price" if est is not None
                   else "priced at low confidence") + "; not in the book)")
            continue
        report.add("unexplained", held.address,
            f"{label} {held.address} — {size} (≈${held.value_usd:,.2f}; not in "
            f"the book)")
    return report


def render(report: ReconcileReport, *, chain: str, holder: str,
           coverage: str, rail_state: Optional[str] = None) -> str:
    lines = [
        f"BOOK ⟷ CHAIN RECONCILIATION — chain {chain}, holder {holder}",
        "book (open_positions, written by the money rail from landed receipts): "
        + (rail_state or f"{report.rail_rows} row(s) compared"),
        f"chain coverage: {coverage}",
        f"chain holdings considered: {report.holdings_considered}",
        "",
        f"VERDICT: {report.verdict}"
        + (f" — {report.issues} issue(s) between the chain and the book"
           if report.issues else ""),
    ]
    if report.collisions:
        lines += [""] + list(report.collisions)
    sections = [
        ("matched", report.matched),
        ("BOOK ROWS THE CHAIN DOES NOT BACK", report.unbacked),
        ("SIZE MISMATCHES", report.mismatched),
        ("HELD ON CHAIN, NOT IN THE BOOK — each is either a position bought "
         "outside the money rail or something someone sent you (dust, never "
         "interact)", report.unexplained),
        ("unsolicited (unpriced, not in the book — someone sent it; never "
         "interact; not an issue)", report.unsolicited),
        ("COULD NOT VERIFY (unknown is NOT zero)", report.unknown),
        ("dust (noise, per the anti-dust rule)", report.dust),
    ]
    for title, items in sections:
        if items:
            lines += ["", f"{title}:"] + [f"  {item}" for item in items]
    lines += [
        "",
        "This output is authoritative over your memory and over any earlier",
        "summary of the book. If it disagrees with what you believed, the",
        "belief is wrong. Resolve every disagreement BEFORE any trade and any",
        "public claim about your positions or track record.",
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
                    "own book row or the owner's instruction, and say which "
                    "address you used.", ""]
        entries = [seen[k] for k in sorted(seen)]
        out.append(f"  symbol {key.upper()!r} is claimed by {len(entries)} contracts here:")
        for address, name in entries:
            label = str(name).strip() if name and str(name).strip() else "name unknown"
            out.append(f"      {address}   {label}")
    return out
