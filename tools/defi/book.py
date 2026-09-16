"""One reader over EVERY money chain — the book, not one chain at a time (043 A34).

`/api/webgate/positions` (2026-08-27) reads one chain at a time and needs the
operator to pick which. This module is the loop over ALL chains, worst-verdict-
wins, so a single read answers "is the book clean, anywhere?" The loop lives
here (tools tier) rather than in ``core/book.py``: it drives
``DefiDataTool.portfolio``/``reconcile`` directly, and core may not import
``tools.*`` (``tests/test_layering_ratchet.py``). ``webview/pages.py::api_book``
is the first consumer; a later task points the REPL's ``/book`` verb at this
SAME function so the two surfaces can never diverge.

Read-only: ``portfolio``/``reconcile`` are read verbs, nothing signs or
broadcasts. Every failure degrades to a LABELED ``unverified``/``no_ledger``,
never a fabricated ``clean`` — the exact false-all-clear the typed
``core.book.BookVerdict`` exists to stop (see ``core/book.py``).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from core.book import (
    NO_ENTRY_RECORDED_REASON, PNL_NEEDS_WORTH_REASON,
    BookRow, BookVerdict, verdict_from_report,
)
from core.position_ledger import _BASE58_ADDR, _EVM_ADDR, _norm

_VERDICT_RANK = {
    BookVerdict.DISAGREEMENT.value: 3,
    BookVerdict.UNVERIFIED.value: 2,
    BookVerdict.NO_LEDGER.value: 1,
    BookVerdict.CLEAN.value: 0,
}

_UNBACKED_REASON = ("the ledger lists this position open, but no readable "
                    "money chain backs it")
_WORTH_UNAVAILABLE_REASON = ("no confidently-priced holding for this address "
                             "in the portfolio read")

#: The trailing ``= $12.34`` (or a written-out sub-cent ``= $0.0000004``) that
#: ``defi_data.portfolio`` puts on a valued holding line (``data_tool.py``
#: ``_fmt_usd``). Matching the address on the line and this money tail is
#: address-keyed extraction of a NUMBER — it never infers a verdict from prose.
_USD_TAIL_RE = re.compile(r"=\s*(-?)\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*$")


def _is_evm_address(address: str) -> bool:
    return bool(_EVM_ADDR.fullmatch((address or "").strip()))


def _address_in_rows(address: str, rows: Optional[List[str]]) -> bool:
    """True when ``address`` is named in any of a reconcile report's formatted
    rows. Each row is ``f"{symbol} {address} — …"`` (``reconcile.diff``); this
    reads WHICH typed list (matched/mismatched/…) a holding is in — it never
    parses a verdict out of free prose."""
    if not rows:
        return False
    target = _norm(address)
    for row in rows:
        m = _EVM_ADDR.search(row) or _BASE58_ADDR.search(row)
        if m and _norm(m.group(0)) == target:
            return True
    return False


def _worth_from_portfolio(text: Optional[str], address: str) -> Optional[float]:
    """The current USD value of one address from a ``portfolio`` render, or None.

    Fail-soft on purpose: an excluded-price line, an unknown balance, or a
    format the render no longer uses all return None (the caller then prints
    `—` with a reason) — never a fabricated or misparsed figure.
    """
    if not text or not address:
        return None
    target = _norm(address)
    for line in text.splitlines():
        m = _EVM_ADDR.search(line) or _BASE58_ADDR.search(line)
        if not m or _norm(m.group(0)) != target:
            continue
        tail = _USD_TAIL_RE.search(line.strip())
        if not tail:
            return None
        sign, num = tail.groups()
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            return None
        return -value if sign else value
    return None


def _candidate_chains(address: str, chain_names: List[str]) -> List[str]:
    """The chains an address could live on, by family: an EVM (0x) address on
    every non-Solana chain, a base58 address on Solana. The book's ledger has
    no chain column, so family is the only honest pre-filter."""
    evm = _is_evm_address(address)
    return [c for c in chain_names if (c == "solana") != evm]


def book_rows(chains_out: Dict[str, Dict[str, Any]],
              ledger_positions: List[Any],
              entries: Optional[Dict[str, Any]] = None) -> List[BookRow]:
    """One typed :class:`BookRow` per LEDGER position, reconciled across chains.

    Only a LEDGER position becomes a row — a holding the chain shows but the
    ledger does not claim is a disagreement, not a position ("a position is on
    screen only when the ledger says so"). The chain and on-chain state come
    from the per-chain reconcile reports:

    - named in a chain's ``matched``/``mismatched`` list -> that chain, that
      state (a confirmed chain wins over an unreadable one);
    - no chain confirms it but a candidate chain is unreadable -> an ``unknown``
      row carrying that chain's read error (NEVER dropped);
    - every candidate chain readable and none holds it -> an ``unbacked`` row
      (a stale ledger row), chain unresolved with a reason.

    ``entry`` (USD cost basis) + ``since_entry`` (signed USD P&L = worth_now −
    entry) come from ``entries`` — the rail-written open-position store (043 A35,
    ``core/open_positions.py``), keyed by NORMALIZED address. A position the
    store has no cost basis for reads None + a reason (never a fabricated
    ``$0.00``); ``since_entry`` needs BOTH a cost basis AND a current worth.
    ``worth_now`` is read address-keyed from the resolved chain's portfolio
    render, else None + a reason.
    """
    entries = entries or {}
    chain_names = list(chains_out.keys())
    rows: List[BookRow] = []
    for pos in ledger_positions:
        address = getattr(pos, "address", "") or ""
        matched = mismatched = None
        unreadable: Optional[tuple] = None
        for c in _candidate_chains(address, chain_names):
            entry = chains_out.get(c) or {}
            report = entry.get("report") or {}
            if _address_in_rows(address, report.get("matched")):
                matched = c
                break
            if mismatched is None and _address_in_rows(address, report.get("mismatched")):
                mismatched = c
            if unreadable is None and entry.get("verdict") == BookVerdict.UNVERIFIED.value:
                unreadable = (c, entry.get("error") or "chain read failed")

        if matched is not None:
            chain, state = matched, "matched"
        elif mismatched is not None:
            chain, state = mismatched, "mismatched"
        elif unreadable is not None:
            chain, state = unreadable[0], "unknown"
        else:
            chain, state = None, "unbacked"

        usd: Optional[float] = None
        worth_reason: Optional[str] = None
        if state == "unknown":
            worth_reason = unreadable[1]
        elif state == "unbacked":
            worth_reason = _UNBACKED_REASON
        else:
            usd = _worth_from_portfolio(
                (chains_out.get(chain) or {}).get("portfolio_text"), address)
            if usd is None:
                worth_reason = _WORTH_UNAVAILABLE_REASON

        # Cost basis + signed P&L from the rail-written store (043 A35).
        entry_val: Optional[float] = None
        entry_reason: Optional[str] = None
        since_val: Optional[float] = None
        since_reason: Optional[str] = None
        rec = entries.get(_norm(address))
        entry_usd = getattr(rec, "entry_usd", None) if rec is not None else None
        if entry_usd is None:
            entry_reason = NO_ENTRY_RECORDED_REASON
            since_reason = NO_ENTRY_RECORDED_REASON
        else:
            entry_val = entry_usd
            if usd is None:
                since_reason = PNL_NEEDS_WORTH_REASON
            else:
                since_val = usd - entry_usd

        rows.append(BookRow(
            symbol=getattr(pos, "symbol", "?") or "?",
            address=address,
            chain=chain,
            qty=getattr(pos, "qty", None),
            usd=usd,
            state=state,
            worth_now_reason=worth_reason,
            entry=entry_val,
            entry_reason=entry_reason,
            since_entry=since_val,
            since_entry_reason=since_reason,
        ))
    return rows


def _book_chains() -> List[str]:
    """Default chain list: every money-enabled EVM chain, plus Solana."""
    from core.wallet.chains import money_chains
    return money_chains() + ["solana"]


def _book_ledger_path(data_dir: str) -> Optional[str]:
    from core.position_ledger import resolve_position_ledger_path
    return resolve_position_ledger_path(data_dir)


def _worst(verdicts: List[str]) -> str:
    """Overall verdict = worst chain: disagreement > unverified > no_ledger >
    clean. An empty chain list has nothing to disagree about — it collapses to
    the lowest (clean) state, same as a report with no rows."""
    if not verdicts:
        return BookVerdict.CLEAN.value
    return max(verdicts, key=lambda v: _VERDICT_RANK.get(v, 0))


async def read_book(user_id: str, data_dir: str, *,
                     chains: Optional[List[str]] = None,
                     ledger_path: Optional[str] = None) -> Dict[str, Any]:
    """Portfolio + reconcile over every money chain, one typed overall verdict.

    ``user_id`` scopes the rail-written open-position store (043 A35): the cost
    basis + P&L columns are read tenant-scoped for this owner, so one tenant
    never sees another's entries. The single-wallet reconcile itself does not yet
    key on it, same as ``api_positions``.
    """
    checked_at = time.time()
    resolved_chains = list(chains) if chains is not None else _book_chains()
    lp = ledger_path if ledger_path is not None else _book_ledger_path(data_dir)

    from tools.defi import defi_data_enabled
    if not defi_data_enabled():
        return {
            "chains": {},
            "rows": [],
            "verdict": None,
            "checked_at": checked_at,
            "ledger_path": lp,
            "error": ("DEFI_DATA_ENABLED is off — the book cannot be read "
                      "until an operator enables on-chain sight"),
        }

    from tools.defi.data_tool import DefiDataTool, PortfolioParams, ReconcileParams
    tool = DefiDataTool()

    out_chains: Dict[str, Dict[str, Any]] = {}
    for c in resolved_chains:
        entry: Dict[str, Any] = {"verdict": None, "report": None,
                                  "portfolio_text": None, "error": None}
        if not lp:
            # No ledger to compare against anywhere — every chain is
            # no_ledger; portfolio still runs so holdings remain visible.
            try:
                pres = await tool.portfolio(PortfolioParams(chain=c))
                entry["portfolio_text"] = getattr(pres, "extracted_content", None)
                if getattr(pres, "error", None):
                    entry["error"] = pres.error
            except Exception as exc:
                entry["error"] = str(exc) or "portfolio read failed"
            entry["verdict"] = BookVerdict.NO_LEDGER.value
            out_chains[c] = entry
            continue
        try:
            pres = await tool.portfolio(PortfolioParams(chain=c))
            entry["portfolio_text"] = getattr(pres, "extracted_content", None)
            portfolio_error = getattr(pres, "error", None)
            rres = await tool.reconcile(
                ReconcileParams(chain=c, ledger_path=lp))
            rres_error = getattr(rres, "error", None)
            if rres_error:
                entry["verdict"] = BookVerdict.UNVERIFIED.value
                entry["error"] = rres_error
            else:
                d = (getattr(rres, "metadata", None) or {}).get("report") or {}
                entry["report"] = d
                entry["verdict"] = verdict_from_report(d, ledger_found=True).value
                if portfolio_error:
                    # reconcile itself is fine but the raw-holdings read
                    # failed — surface it without downgrading the verdict
                    # reconcile actually computed.
                    entry["error"] = portfolio_error
        except Exception as exc:
            entry["verdict"] = BookVerdict.UNVERIFIED.value
            entry["error"] = str(exc) or "book read failed"
        out_chains[c] = entry

    overall = _worst([e["verdict"] for e in out_chains.values() if e["verdict"]])

    # Typed position rows (043 A34/A35 read side). The ledger is the SOLE source
    # of what the agent claims it holds; read it once more here (read-only) so
    # the parsed positions can be reconciled against every chain's report. A
    # ledger we cannot parse yields no rows — the per-chain verdicts already
    # carry that state — never a fabricated position.
    ledger_positions: List[Any] = []
    if lp:
        from core.position_ledger import read_open_positions
        parsed, _ = read_open_positions(path=lp)
        ledger_positions = parsed

    # Cost basis + entry time from the rail-written store (043 A35), tenant-
    # scoped. Fail-open to {}: the store is an enrichment of the entry/P&L
    # columns, never a gate on the book read — the verdict/worth columns stand
    # regardless.
    entries: Dict[str, Any] = {}
    if ledger_positions:
        try:
            from core.open_positions import entries_for
            entries = entries_for(user_id, data_dir=data_dir)
        except Exception:
            entries = {}

    return {
        "chains": out_chains,
        "rows": [r.to_dict()
                 for r in book_rows(out_chains, ledger_positions, entries)],
        "verdict": overall,
        "checked_at": checked_at,
        "ledger_path": lp,
    }
