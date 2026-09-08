"""Position-ledger reading primitives — the core-tier half of `tools/defi/reconcile`.

Relocated here 2026-08-28 (status SSOT) for the same reason `core/config_policy`
was carved out of `agents.task.constants`: `core/status_snapshot.py` must be able
to say how many positions the agent believes it holds, and `core` (tier 0) may not
import `tools` (tier 3). The parser is pure — regex over markdown, no I/O — so it
belongs in the lower tier; `tools/defi/reconcile.py` re-exports every symbol, and
the trading-specific comparison (`ChainHolding`/`diff`/`render`) stays there.

Why the status surface reads this at all: on 2026-08-25 the agent published
"book flat" to X while holding three positions. On 2026-08-28 `/status` reported
`treasury cash flow … net $+0.00` with a parenthetical that positions were "NOT
included" — true, and useless: an owner reading it cannot tell whether the book
is empty or has two unsellable bags in it (it had two). A count the owner can see
is the cheap half of that fix; the expensive half (verifying it against chain)
stays with the `reconcile` verb.

Contract: **UNKNOWN is never zero.** A ledger we cannot find or parse is reported
as unavailable, never as "no open positions".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

_EVM_ADDR = re.compile(r"0x[0-9a-fA-F]{40}")
_BASE58_ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

#: Default filename the trading rail's ledger uses; `POSITION_LEDGER_PATH`
#: overrides it outright, and any `*position-ledger.md` in the project dir is
#: accepted as a fallback so a renamed ledger degrades to "found" rather than
#: to a silent zero.
DEFAULT_LEDGER_NAME = "kb-root-position-ledger.md"
_LEDGER_GLOB = "*position-ledger.md"


def _norm(address: str) -> str:
    """Comparison key: EVM hex is case-insensitive, base58 is case-SENSITIVE."""
    a = (address or "").strip()
    return a.lower() if a.startswith("0x") or a.startswith("0X") else a


@dataclass
class LedgerPosition:
    symbol: str
    address: str
    qty: Optional[float]  # None = row present but size unparseable
    line: str


def _parse_qty(cell: str) -> Optional[float]:
    cleaned = cell.strip().strip("*`").replace(",", "").replace("~", "")
    cleaned = cleaned.lstrip("$").strip()
    if not cleaned:
        return None
    m = re.match(r"^-?\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_open_positions(text: str) -> Tuple[List[LedgerPosition], Optional[str]]:
    """Rows of the `## Open positions` table, or (rows, error).

    Only the STATE TABLE is read, on purpose: the run log below it is narrative,
    and treating narrative as state is how the table went stale unnoticed. If
    the table and the chain disagree, `diff` says so loudly — that pressure is
    what keeps the table maintained.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("##") and "open positions" in line.lower():
            start = i + 1
            break
    if start is None:
        return [], ("the ledger has no '## Open positions' section — the state "
                    "table is missing, so there is nothing to reconcile against")
    rows: List[LedgerPosition] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if not stripped.startswith("|"):
            continue
        bare = stripped.strip("|").replace("|", "").replace("-", "").replace(":", "").strip()
        if not bare:
            continue  # separator row
        addr_match = _EVM_ADDR.search(stripped) or _BASE58_ADDR.search(stripped)
        if addr_match is None:
            continue  # header row, "(none — book flat)" placeholder, prose
        address = addr_match.group(0)
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        addr_idx = next((i for i, c in enumerate(cells) if address in c), None)
        qty = None
        if addr_idx is not None:
            for cell in cells[addr_idx + 1:]:
                qty = _parse_qty(cell)
                if qty is not None:
                    break
        symbol = (cells[0].strip("*` ") if cells else "") or "?"
        rows.append(LedgerPosition(symbol=symbol, address=address, qty=qty,
                                   line=stripped))
    return rows, None


def resolve_position_ledger_path(data_dir: Optional[str] = None) -> Optional[str]:
    """Best-effort path to the LIVE position ledger, or None.

    Precedence: ``POSITION_LEDGER_PATH`` (explicit, wins even if missing so a
    typo surfaces as "not found" rather than being papered over by the glob) →
    ``<project dir>/kb-root-position-ledger.md`` → the single ``*position-ledger.md``
    in the project dir. The project dir is ``POLYROB_PROJECT_DIR`` else
    ``<data_dir>/project``.

    Returns None only when no candidate exists — the caller must render that as
    unavailable/unknown, never as an empty book.
    """
    explicit = (os.getenv("POSITION_LEDGER_PATH") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    project = (os.getenv("POLYROB_PROJECT_DIR") or "").strip()
    if not project:
        if not data_dir:
            return None
        project = os.path.join(str(data_dir), "project")
    default = os.path.join(project, DEFAULT_LEDGER_NAME)
    if os.path.isfile(default):
        return default
    try:
        import glob
        hits = sorted(p for p in glob.glob(os.path.join(project, _LEDGER_GLOB))
                      if os.path.isfile(p))
    except OSError:
        return None
    # More than one candidate is ambiguous — refuse to guess which book is live.
    return hits[0] if len(hits) == 1 else None


def read_open_positions(data_dir: Optional[str] = None,
                        path: Optional[str] = None) -> Tuple[List[LedgerPosition], Optional[str]]:
    """``(rows, error)`` for the live ledger. ``error`` non-None means UNKNOWN —
    the caller must not render it as zero positions."""
    ledger = path or resolve_position_ledger_path(data_dir)
    if not ledger:
        return [], "no position ledger found (set POSITION_LEDGER_PATH)"
    try:
        if os.path.getsize(ledger) > 1_000_000:
            return [], f"{os.path.basename(ledger)} exceeds 1MB — not a position ledger"
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        return [], f"{type(e).__name__}: {e}"
    return parse_open_positions(text)
