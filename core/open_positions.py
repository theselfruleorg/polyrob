"""The rail-written open-position store (043 A35) — core tier.

`core/position_ledger.py` PARSES the agent-authored markdown ledger (symbol /
address / qty); it is pure, regex-only, no I/O. What it cannot answer is *what a
position cost*: the ledger has no entry column, so the Money Book's "Entry" (cost
basis) and "Since entry" (signed USD profit/loss) read `—` with a reason. This
module is the missing writer: every value-moving swap the money rail records
(``core/wallet/policy.py::PolicyGate.record``) also writes/updates an open
position here, so the book finally has a truthful cost basis to compute P&L from.

Why a second store and not `position_ledger.py`: that module's contract is
"pure — no I/O", and the status snapshot leans on it staying cheap and pure.
A sqlite writer belongs beside it, not inside it.

Contract, mirroring the bridge/goal stores in this tier:
- WAL + jittered retry via ``core/sqlite_util`` (concurrent readers + one writer).
- TENANT-SCOPED: every row carries ``user_id``; a read for a tenant never sees
  another tenant's positions. An empty/anonymous tenant is refused a WRITE (a
  position with no owner is a row no surface could ever tenant-scope back).
- REACH, NOT POLICY: the write is ADDITIVE and fail-open. The spend is booked
  exactly as today; losing this write must never break a recorded trade, and it
  never touches a gate decision, a cap, or a refusal.
- A READ never CREATES the store (the status-SSOT rule): no file = no position
  ever recorded, a real answer; a read must not leave an empty db behind.
- NO ROW WITHOUT A REAL TRADE: the writer is only ever reached from
  ``PolicyGate.record`` after a confirmed broadcast.

Cost-basis math (weighted, USD):
- an ACQUIRE (``qty>0``) adds its cost to ``entry_usd`` and its size to ``qty``;
  the first acquire stamps ``entry_ts``;
- a DISPOSE (``qty<0``) reduces ``qty`` and reduces ``entry_usd`` in the SAME
  proportion, so the remaining cost basis still describes the remaining size; a
  full (or over-) sell CLOSES the row (deleted);
- a dispose of a position no row holds is a NO-OP — the rail never opens a
  negative/short position, and the spend is still booked normally either way.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.position_ledger import _norm

logger = logging.getLogger(__name__)

_DB_NAME = "open_positions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS open_positions (
    user_id     TEXT NOT NULL,
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    symbol      TEXT,
    qty         REAL NOT NULL,
    entry_usd   REAL NOT NULL,
    entry_ts    REAL NOT NULL,
    updated_ts  REAL NOT NULL,
    PRIMARY KEY (user_id, chain, address)
);
CREATE INDEX IF NOT EXISTS idx_open_positions_addr
    ON open_positions(user_id, address);
"""

#: A sell within this fraction of the held size is a full exit — the row is
#: closed rather than left holding a dust remainder (a rounding overshoot must
#: not leave a $0.0000001 position on the book forever).
_CLOSE_EPS = 1e-9


@dataclass(frozen=True)
class PositionDelta:
    """One leg of a trade's effect on a tracked position.

    ``qty`` is SIGNED: ``>0`` acquires (opens/increases), ``<0`` disposes
    (reduces/closes). ``cost_usd`` is the USD paid for an acquisition and is
    ignored on a dispose. ``address`` is the position token — NOT the DEX spender
    the spend was recorded against.
    """
    chain: str
    address: str
    symbol: str
    qty: float
    cost_usd: Optional[float] = None


@dataclass(frozen=True)
class PositionEntry:
    """What the Money Book reads back for one position: the cost basis
    (``entry_usd``) and when it was first opened (``entry_ts``)."""
    chain: str
    address: str
    symbol: Optional[str]
    qty: float
    entry_usd: float
    entry_ts: float


# --------------------------------------------------------------------------
# value-asset classification + swap -> deltas (PURE)
# --------------------------------------------------------------------------

def _is_value_asset(chain: str, address: Optional[str], is_native: bool) -> bool:
    """True when this side is working capital (native gas or a pinned
    stablecoin / wrapped-native), not a position.

    Mirrors ``tools/defi/reconcile.py``'s rule that the quote asset (USDC) and
    the wrapped native are working capital, never positions — so a USDC->MEME
    buy opens a MEME position (not a USDC one) and a MEME->USDC sell closes the
    MEME position (not opens a USDC one).
    """
    if is_native:
        return True
    if not address:
        return True
    try:
        from core.wallet.tokens import canonical_token
        return canonical_token(chain, address) is not None
    except Exception:
        # Fail-open toward "not value": a token we cannot classify is tracked as
        # a position rather than silently dropped from the book.
        return False


def classify_swap(*, chain: str, token_in: str, token_out: str,
                  in_native: bool, in_symbol: str, out_symbol: str,
                  in_qty: float, out_qty: Optional[float],
                  cost_usd: Optional[float]) -> List[PositionDelta]:
    """Turn one swap into 0-2 :class:`PositionDelta` legs.

    - the token BOUGHT (``token_out``) opens/increases a position when it is not
      working capital — its cost basis is ``cost_usd`` (the USD the swap spent);
    - the token SOLD (``token_in``) reduces/closes a position when it is not
      working capital;
    - a value<->value swap (e.g. USDC<->WETH) tracks nothing.

    ``out_qty`` may be None (an aggregator that did not report a human output);
    without a size we cannot track the acquired position's close math, so that
    leg is dropped rather than written with an unknown quantity.
    """
    deltas: List[PositionDelta] = []
    if (out_qty is not None and out_qty > 0
            and not _is_value_asset(chain, token_out, is_native=False)):
        deltas.append(PositionDelta(
            chain=chain, address=_norm(token_out),
            symbol=out_symbol or (token_out or "?"),
            qty=float(out_qty),
            cost_usd=(None if cost_usd is None else float(cost_usd))))
    if (in_qty and in_qty > 0
            and not _is_value_asset(chain, token_in, is_native=in_native)):
        deltas.append(PositionDelta(
            chain=chain, address=_norm(token_in),
            symbol=in_symbol or (token_in or "?"),
            qty=-float(in_qty), cost_usd=None))
    return deltas


# --------------------------------------------------------------------------
# store I/O
# --------------------------------------------------------------------------

def open_positions_db_path(data_dir: Optional[str] = None) -> str:
    """``<data_dir or data_home>/open_positions.db``. Resolved at CALL time,
    never bound at import (``tests/test_home_binding_ratchet.py``)."""
    if data_dir:
        return os.path.join(str(data_dir), _DB_NAME)
    from core.runtime_paths import resolve_data_home
    return os.path.join(str(resolve_data_home()), _DB_NAME)


def _init(db_path: str) -> None:
    from core.sqlite_util import wal_connect
    conn = wal_connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def apply_delta(user_id: str, delta: PositionDelta, *,
                db_path: Optional[str] = None,
                now: Optional[float] = None) -> None:
    """Apply one acquire/dispose leg to the tenant's open position. Fail-open.

    Never raises into the caller: a money verb's recorded spend must stand even
    if this bookkeeping write fails.
    """
    uid = str(user_id or "").strip()
    if not uid:
        # A position with no owner is a row no surface can tenant-scope back.
        return
    if not delta.address:
        return
    try:
        from core.sqlite_util import execute_retry
        path = db_path or open_positions_db_path()
        _init(path)
        ts = time.time() if now is None else now
        row = execute_retry(
            path,
            "SELECT qty, entry_usd, entry_ts FROM open_positions "
            "WHERE user_id=? AND chain=? AND address=?",
            (uid, delta.chain, delta.address), fetch="one")
        cur_qty = float(row["qty"]) if row else 0.0
        cur_entry = float(row["entry_usd"]) if row else 0.0
        cur_entry_ts = float(row["entry_ts"]) if row else ts

        if delta.qty > 0:  # acquire (open / increase)
            new_qty = cur_qty + delta.qty
            new_entry = cur_entry + float(delta.cost_usd or 0.0)
            execute_retry(
                path,
                "INSERT OR REPLACE INTO open_positions "
                "(user_id, chain, address, symbol, qty, entry_usd, entry_ts, "
                "updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (uid, delta.chain, delta.address, delta.symbol, new_qty,
                 new_entry, cur_entry_ts, ts))
            return

        # dispose (reduce / close)
        if not row or cur_qty <= 0:
            return  # nothing held — never open a negative/short position
        sell = -delta.qty
        if sell >= cur_qty - abs(cur_qty) * _CLOSE_EPS:
            execute_retry(
                path,
                "DELETE FROM open_positions "
                "WHERE user_id=? AND chain=? AND address=?",
                (uid, delta.chain, delta.address))
            return
        remaining = cur_qty - sell
        # Reduce the cost basis in the SAME proportion as the size, so the
        # entry_usd left still describes the size left.
        new_entry = cur_entry * (remaining / cur_qty)
        execute_retry(
            path,
            "UPDATE open_positions SET qty=?, entry_usd=?, updated_ts=? "
            "WHERE user_id=? AND chain=? AND address=?",
            (remaining, new_entry, ts, uid, delta.chain, delta.address))
    except Exception:
        logger.debug("open-position write skipped (fail-open)", exc_info=True)


def apply_deltas(user_id: str, deltas: List[PositionDelta], *,
                 db_path: Optional[str] = None,
                 now: Optional[float] = None) -> None:
    """Apply every leg of a trade. Fail-open per leg."""
    for d in deltas or []:
        apply_delta(user_id, d, db_path=db_path, now=now)


def get_position(user_id: str, chain: str, address: str, *,
                 db_path: Optional[str] = None) -> Optional[PositionEntry]:
    """One tenant-scoped position by ``(chain, address)``, or None. Read-only —
    never creates the store."""
    uid = str(user_id or "").strip()
    if not uid:
        return None
    path = db_path or open_positions_db_path()
    if not os.path.isfile(path):
        return None
    try:
        from core.sqlite_util import execute_retry
        row = execute_retry(
            path,
            "SELECT chain, address, symbol, qty, entry_usd, entry_ts "
            "FROM open_positions WHERE user_id=? AND chain=? AND address=?",
            (uid, chain, _norm(address)), fetch="one")
    except Exception:
        logger.warning("open-position read failed for %s", uid, exc_info=True)
        return None
    if not row:
        return None
    return PositionEntry(
        chain=row["chain"], address=row["address"], symbol=row["symbol"],
        qty=float(row["qty"]), entry_usd=float(row["entry_usd"]),
        entry_ts=float(row["entry_ts"]))


def entries_for(user_id: str, *, data_dir: Optional[str] = None,
                db_path: Optional[str] = None) -> Dict[str, PositionEntry]:
    """Every open position for a tenant, keyed by NORMALIZED address.

    What the Money Book joins its ledger positions against (the ledger has no
    chain column, so the address is the honest join key). Read-only, fail-open
    to ``{}`` — an unreadable store degrades the "Entry"/"Since entry" columns to
    "no entry recorded", never breaks the book read; the primary verdict/worth
    columns are unaffected.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return {}
    path = db_path or open_positions_db_path(data_dir)
    if not os.path.isfile(path):
        return {}
    try:
        from core.sqlite_util import execute_retry
        rows = execute_retry(
            path,
            "SELECT chain, address, symbol, qty, entry_usd, entry_ts "
            "FROM open_positions WHERE user_id=? ORDER BY updated_ts ASC",
            (uid,), fetch="all") or []
    except Exception:
        logger.warning("open-position read failed for %s", uid, exc_info=True)
        return {}
    out: Dict[str, PositionEntry] = {}
    for r in rows:
        # A later-updated row for the same address wins (ORDER BY updated_ts ASC).
        out[_norm(r["address"])] = PositionEntry(
            chain=r["chain"], address=r["address"], symbol=r["symbol"],
            qty=float(r["qty"]), entry_usd=float(r["entry_usd"]),
            entry_ts=float(r["entry_ts"]))
    return out
