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
broadcasts. The positions are the rail-written book (``core/open_positions.py``).
Every failure degrades to a LABELED ``unverified``, never a fabricated ``clean`` — the exact false-all-clear the typed
``core.book.BookVerdict`` exists to stop (see ``core/book.py``).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from core.book import (
    BASIS_UNKNOWN_REASON, PNL_NEEDS_WORTH_REASON, BookRow, BookVerdict,
    verdict_from_report,
)
from core.open_positions import _norm
from tools.defi.reconcile import _BASE58_ADDR, _EVM_ADDR

_VERDICT_RANK = {
    BookVerdict.DISAGREEMENT.value: 3,
    BookVerdict.UNVERIFIED.value: 2,
    BookVerdict.NO_LEDGER.value: 1,
    BookVerdict.CLEAN.value: 0,
}

_UNBACKED_REASON = ("the book lists this position open, but its chain does "
                    "not back it")
_WORTH_UNAVAILABLE_REASON = ("no confidently-priced holding for this address "
                             "in the portfolio read")

#: The trailing ``= $12.34`` (or a written-out sub-cent ``= $0.0000004``) that
#: ``defi_data.portfolio`` puts on a valued holding line (``data_tool.py``
#: ``_fmt_usd``). Matching the address on the line and this money tail is
#: address-keyed extraction of a NUMBER — it never infers a verdict from prose.
_USD_TAIL_RE = re.compile(r"=\s*(-?)\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*$")


def _is_evm_address(address: str) -> bool:
    return bool(_EVM_ADDR.fullmatch((address or "").strip()))


def _row_address(row: str) -> Optional[str]:
    """The ADDRESS field of a legacy ``f"{symbol} {address} — …"`` row: the last
    whitespace token before the first `` — ``. Used only when a report carries
    no structured ``addresses`` (hand-built reports); never "the first
    address-shaped text in the line", which a symbol could supply (CR-L11)."""
    head = (row or "").split(" — ", 1)[0].split()
    if not head:
        return None
    token = head[-1]
    if _EVM_ADDR.fullmatch(token) or _BASE58_ADDR.fullmatch(token):
        return token
    return None


def _address_in_rows(address: str, rows: Optional[List[str]],
                     addresses: Optional[List[str]] = None) -> bool:
    """True when ``address`` is named in any of a reconcile report's formatted
    rows. Each row is ``f"{symbol} {address} — …"`` (``reconcile.diff``); this
    reads WHICH typed list (matched/mismatched/…) a holding is in — it never
    parses a verdict out of free prose.

    CR-L11: matched by the report's structured ``addresses`` list when present
    (``ReconcileReport.addresses``), else by the row's address FIELD — never by
    the first address in the line, because the symbol comes first and a symbol
    is attacker-authored."""
    target = _norm(address)
    if addresses is not None:
        return any(_norm(a) == target for a in addresses if a)
    if not rows:
        return False
    for row in rows:
        found = _row_address(row)
        if found and _norm(found) == target:
            return True
    return False


def _worth_from_metadata(metadata: Any, address: str) -> Optional[float]:
    """071 W1: the typed ``usd`` of one holding from ``portfolio`` metadata.
    Set only for a row the tool itself valued (counted in ``total_usd``); any
    other row — excluded, unpriced, budget-skipped — or an absent one is None."""
    target = _norm(address)
    for row in (metadata.get("holdings") or []):
        if not isinstance(row, dict) or _norm(str(row.get("address") or "")) != target:
            continue
        usd = row.get("usd")
        return float(usd) if isinstance(usd, (int, float)) and row.get("status") == "valued" else None
    return None


def _worth_from_portfolio(text: Optional[str], address: str,
                          metadata: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """The current USD value of one address from a ``portfolio`` result, or None.

    071 W1: the typed ``metadata`` is read first; the prose regex below is the
    fallback for a result that carries none.

    Fail-soft on purpose: an excluded-price line, an unknown balance, or a
    format the render no longer uses all return None (the caller then prints
    `—` with a reason) — never a fabricated or misparsed figure.

    CR-L11: a holding line is ``  {address}  {amount} {symbol}  = $X``; the
    address is the line's FIRST field and nothing else on the line counts.
    """
    if isinstance(metadata, dict) and isinstance(metadata.get("holdings"), list) and address:
        return _worth_from_metadata(metadata, address)
    if not text or not address:
        return None
    target = _norm(address)
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        first = fields[0]
        if not (_EVM_ADDR.fullmatch(first) or _BASE58_ADDR.fullmatch(first)):
            continue
        if _norm(first) != target:
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
    every non-Solana chain, a base58 address on Solana. Used only for a
    position whose own chain is not in the read."""
    evm = _is_evm_address(address)
    return [c for c in chain_names if (c == "solana") != evm]


def book_rows(chains_out: Dict[str, Dict[str, Any]],
              positions: List[Any]) -> List[BookRow]:
    """One typed :class:`BookRow` per BOOK position (``core/open_positions.py``
    rows), reconciled on its own chain.

    The on-chain state comes from that chain's reconcile report:

    - named in its ``matched``/``mismatched`` list -> that state;
    - the chain is unreadable -> an ``unknown`` row carrying the read error
      (NEVER dropped);
    - the chain is readable and does not hold it -> an ``unbacked`` row.

    ``entry`` (USD cost basis) is the row's own; ``since_entry`` (signed USD
    P&L = worth_now − entry) needs BOTH a cost basis AND a current worth. A NULL
    basis reads None + a reason, never ``$0.00``. ``worth_now`` is read
    address-keyed from the chain's portfolio render, else None + a reason.
    """
    chain_names = list(chains_out.keys())
    rows: List[BookRow] = []
    for pos in positions:
        address = getattr(pos, "address", "") or ""
        own = str(getattr(pos, "chain", "") or "").strip().lower()
        candidates = [own] if own in chains_out else _candidate_chains(address, chain_names)
        matched = mismatched = None
        unreadable: Optional[tuple] = None
        for c in candidates:
            entry = chains_out.get(c) or {}
            report = entry.get("report") or {}
            structured = report.get("addresses")
            structured = structured if isinstance(structured, dict) else None

            def _in(kind: str) -> bool:
                return _address_in_rows(
                    address, report.get(kind),
                    None if structured is None else (structured.get(kind) or []))

            if _in("matched"):
                matched = c
                break
            if mismatched is None and _in("mismatched"):
                mismatched = c
            if unreadable is None and entry.get("verdict") == BookVerdict.UNVERIFIED.value:
                unreadable = (c, entry.get("error") or "chain read failed")
            if unreadable is None and _in("unknown"):
                unreadable = (c, "chain balance unknown (read failed)")

        if matched is not None:
            chain, state = matched, "matched"
        elif mismatched is not None:
            chain, state = mismatched, "mismatched"
        elif unreadable is not None:
            chain, state = unreadable[0], "unknown"
        else:
            chain, state = (own or None), "unbacked"

        usd: Optional[float] = None
        worth_reason: Optional[str] = None
        if state == "unknown":
            worth_reason = unreadable[1]
        elif state == "unbacked":
            worth_reason = _UNBACKED_REASON
        else:
            usd = _worth_from_portfolio(
                (chains_out.get(chain) or {}).get("portfolio_text"), address,
                metadata=(chains_out.get(chain) or {}).get("portfolio_metadata"))
            if usd is None:
                worth_reason = _WORTH_UNAVAILABLE_REASON

        # Cost basis + signed P&L from the book row itself (043 A35).
        entry_val: Optional[float] = None
        entry_reason: Optional[str] = None
        since_val: Optional[float] = None
        since_reason: Optional[str] = None
        entry_usd = getattr(pos, "entry_usd", None)
        if entry_usd is None:
            # 071 W3: a NULL basis is "basis unknown" — never a $0 basis.
            entry_reason = BASIS_UNKNOWN_REASON
            since_reason = BASIS_UNKNOWN_REASON
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
            lifecycle=getattr(pos, "status", None) or "open",
            lifecycle_reason=getattr(pos, "status_reason", None) or None,
        ))
    return rows


def _book_chains() -> List[str]:
    """Default chain list: every money-enabled EVM chain, plus Solana."""
    from core.wallet.chains import money_chains
    return money_chains() + ["solana"]


def _worst(verdicts: List[str]) -> str:
    """Overall verdict = worst chain: disagreement > unverified > no_ledger >
    clean. An empty chain list has nothing to disagree about — it collapses to
    the lowest (clean) state, same as a report with no rows."""
    if not verdicts:
        return BookVerdict.CLEAN.value
    return max(verdicts, key=lambda v: _VERDICT_RANK.get(v, 0))


async def read_book(user_id: str, data_dir: str, *,
                    chains: Optional[List[str]] = None) -> Dict[str, Any]:
    """Portfolio + reconcile over every money chain, one typed overall verdict.

    ``user_id`` scopes the rail-written position book (043 A35): one tenant
    never sees another's positions.
    """
    checked_at = time.time()
    resolved_chains = list(chains) if chains is not None else _book_chains()

    from tools.defi import defi_data_enabled
    if not defi_data_enabled():
        return {
            "chains": {},
            "rows": [],
            "verdict": None,
            "checked_at": checked_at,
            "error": ("DEFI_DATA_ENABLED is off — the book cannot be read "
                      "until an operator enables on-chain sight"),
        }

    # The positions: the rail-written book, tenant-scoped. An existing store
    # that cannot be read is UNKNOWN, never an empty book.
    positions: List[Any] = []
    book_error: Optional[str] = None
    try:
        from core.open_positions import open_positions_db_path, positions_for
        positions = positions_for(user_id, db_path=open_positions_db_path(data_dir))
    except Exception as exc:
        book_error = (f"the position book (open_positions.db) could not be read "
                      f"({type(exc).__name__}) — this is NOT an empty book")

    from tools.defi.data_tool import DefiDataTool, PortfolioParams, ReconcileParams
    tool = DefiDataTool()

    from core.exec_identity import reset_exec_identity, set_exec_identity
    out_chains: Dict[str, Dict[str, Any]] = {}
    token = set_exec_identity(user_id, None)  # reconcile reads THIS tenant's book
    try:
        for c in resolved_chains:
            entry: Dict[str, Any] = {"verdict": None, "report": None,
                                     "portfolio_text": None, "error": None}
            try:
                pres = await tool.portfolio(PortfolioParams(chain=c))
                entry["portfolio_text"] = getattr(pres, "extracted_content", None)
                entry["portfolio_metadata"] = getattr(pres, "metadata", None)
                portfolio_error = getattr(pres, "error", None)
                if book_error:
                    entry["verdict"] = BookVerdict.UNVERIFIED.value
                    entry["error"] = book_error
                    out_chains[c] = entry
                    continue
                rres = await tool.reconcile(ReconcileParams(chain=c))
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
    finally:
        reset_exec_identity(token)

    overall = _worst([e["verdict"] for e in out_chains.values() if e["verdict"]])
    rows = book_rows(out_chains, positions)
    return {
        "chains": out_chains,
        "rows": [r.to_dict() for r in rows],
        "tracked": tracked_rows(rows),
        "verdict": overall,
        "checked_at": checked_at,
        **({"error": book_error} if book_error else {}),
    }


def tracked_rows(rows: List[BookRow]) -> List[Dict[str, Any]]:
    """W1: the book positions that wait on an owner decision — one the chain
    does not back (``/writeoff``), or one the identity gate quarantined
    (``/unquarantine``), or one already written off. ``open_positions.db`` is
    what the identity gate reads, so these rows are shown with their lifecycle."""
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        lifecycle = row.lifecycle or "open"
        if lifecycle == "open" and row.state != "unbacked":
            continue
        out.append({
            "symbol": row.symbol or "?",
            "chain": row.chain,
            "address": row.address,
            "amount": row.qty,
            "entry": row.entry,
            "lifecycle": lifecycle,
            "lifecycle_reason": row.lifecycle_reason,
        })
    return out
