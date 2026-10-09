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
  ``PolicyGate.record`` after a confirmed broadcast — the one exception is
  :func:`seed_inherited`, which records what an agent NFT's account ALREADY held when it was
  adopted (``origin='inherited'``, no cost basis: the book reads it NO_LEDGER).
- THE HOLDER IS PART OF THE KEY (050 §7.4): ``account`` = ``""`` is the
  instance's own treasury wallet (every pre-2026-09-23 row); a token-bound ERC-6551
  account address (lower-case) is its own book. Without it an account and the
  treasury holding the same token would share one row.

Cost-basis math (weighted, USD):
- an ACQUIRE (``qty>0``) adds its cost to ``entry_usd`` and its size to ``qty``;
  the first acquire stamps ``entry_ts``;
- a DISPOSE (``qty<0``) reduces ``qty`` and reduces ``entry_usd`` in the SAME
  proportion, so the remaining cost basis still describes the remaining size; a
  full (or over-) sell CLOSES the row (deleted);
- a dispose of a position no row holds is a NO-OP — the rail never opens a
  negative/short position, and the spend is still booked normally either way.

071 W3 (one book, verbs do the arithmetic):
- ``entry_usd`` is NULLABLE: an unknown basis is NULL, never ``0`` (TM P1-11).
  Every reader renders NULL as "basis unknown". An older store is rebuilt once
  in place (:func:`_nullable_basis`).
- each dispose books what it realized (average cost — the ONE cost method) into
  ``position_realized``, which outlives the deleted row;
- ``qty_source`` records simulation estimates, quotes and legacy event counts;
  ``high_water_usd`` is the highest price a ``positions`` read observed.
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

_TABLE = """
CREATE TABLE IF NOT EXISTS open_positions (
    user_id     TEXT NOT NULL,
    chain       TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL,
    symbol      TEXT,
    qty         REAL NOT NULL,
    entry_usd   REAL,
    entry_ts    REAL NOT NULL,
    updated_ts  REAL NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'trade',
    status        TEXT NOT NULL DEFAULT 'open',
    status_reason TEXT NOT NULL DEFAULT '',
    status_ts     REAL,
    qty_source    TEXT NOT NULL DEFAULT '',
    high_water_usd REAL,
    high_water_ts  REAL,
    PRIMARY KEY (user_id, chain, account, address)
);
"""
_INDEX = """
CREATE INDEX IF NOT EXISTS idx_open_positions_addr
    ON open_positions(user_id, address);
"""
#: 071 W3: what each average-cost reduction realized, per holder and token. A
#: separate table because a full exit DELETES the open row — the realized
#: figure must outlive it. ``realized_usd`` sums only the legs whose basis AND
#: proceeds were both known; ``unknown_legs`` counts the rest, and a reader
#: that sees ``unknown_legs > 0`` must not present the sum as the whole figure.
_REALIZED = """
CREATE TABLE IF NOT EXISTS position_realized (
    user_id      TEXT NOT NULL,
    chain        TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL,
    symbol       TEXT,
    realized_usd REAL NOT NULL DEFAULT 0,
    known_legs   INTEGER NOT NULL DEFAULT 0,
    unknown_legs INTEGER NOT NULL DEFAULT 0,
    sold_qty     REAL NOT NULL DEFAULT 0,
    updated_ts   REAL NOT NULL,
    PRIMARY KEY (user_id, chain, account, address)
);
"""
_SCHEMA = _TABLE + ";\n" + _INDEX + ";\n" + _REALIZED


#: The treasury's own book (every row written before token-bound accounts existed).
TREASURY_ACCOUNT = ""

#: Position lifecycle (W0). ``open`` is every row the rail writes. A
#: ``quarantined`` row is a look-alike the identity gate found claiming the
#: symbol of a TRUSTED token: it keeps its cost basis (honest P&L) but is never a
#: symbol claim again. ``written_off`` is the owner's loss verdict.
STATUS_OPEN = "open"
STATUS_QUARANTINED = "quarantined"
STATUS_WRITTEN_OFF = "written_off"
STATUSES = (STATUS_OPEN, STATUS_QUARANTINED, STATUS_WRITTEN_OFF)

_STATUS_COLUMNS = (("status", "TEXT NOT NULL DEFAULT 'open'"),
                   ("status_reason", "TEXT NOT NULL DEFAULT ''"),
                   ("status_ts", "REAL"))

#: 071 W3 columns, added in place to an older store.
_BOOK_COLUMNS = (("qty_source", "TEXT NOT NULL DEFAULT ''"),
                 ("high_water_usd", "REAL"),
                 ("high_water_ts", "REAL"))

#: Where a row's size came from. ``receipt`` = legacy token-authored Transfer
#: logs (unverified); ``simulation`` = preview balance delta, not final fill; ``quote`` = the
#: quoted output, never measured; ``inherited`` = the balance an adopted
#: account already held; ``""`` = a row written before the column existed.
QTY_SOURCES = ("receipt", "simulation", "quote", "inherited", "")

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
    #: The holder (050 §7.4): ``""`` = the treasury, else the token-bound account.
    account: str = TREASURY_ACCOUNT
    #: 071 W3: the USD a DISPOSE received (``None`` = not known). Ignored on an
    #: acquire. Realized P&L = proceeds − the average-cost basis of the size sold.
    proceeds_usd: Optional[float] = None
    #: Quantity provenance (legacy receipts are unverified); see QTY_SOURCES.
    qty_source: str = "quote"


@dataclass(frozen=True)
class PositionEntry:
    """What the Money Book reads back for one position: the cost basis
    (``entry_usd``) and when it was first opened (``entry_ts``).

    ``entry_usd is None`` = the basis is UNKNOWN (071 W3, TM P1-11). Unknown is
    not zero: render it as "basis unknown", never as $0.00."""
    chain: str
    address: str
    symbol: Optional[str]
    qty: float
    entry_usd: Optional[float]
    entry_ts: float
    account: str = TREASURY_ACCOUNT
    #: ``"trade"`` (the rail bought it) | ``"inherited"`` (held at adopt; no cost basis).
    origin: str = "trade"
    #: ``open`` | ``quarantined`` | ``written_off`` (W0).
    status: str = STATUS_OPEN
    status_reason: str = ""
    #: 071 W3: where ``qty`` came from (QTY_SOURCES).
    qty_source: str = ""
    #: 071 W3: the highest per-token USD price a ``positions`` read observed
    #: while this row was open, and when. None = never observed.
    high_water_usd: Optional[float] = None
    high_water_ts: Optional[float] = None


@dataclass(frozen=True)
class RealizedEntry:
    """071 W3: what the average-cost reductions of one token realized."""
    chain: str
    address: str
    symbol: Optional[str]
    account: str
    realized_usd: float
    known_legs: int
    unknown_legs: int
    sold_qty: float
    updated_ts: float


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
                  cost_usd: Optional[float],
                  qty_source: str = "quote") -> List[PositionDelta]:
    """Turn one swap into 0-2 :class:`PositionDelta` legs.

    - the token BOUGHT (``token_out``) opens/increases a position when it is not
      working capital — its cost basis is ``cost_usd`` (the USD the swap spent);
    - the token SOLD (``token_in``) reduces/closes a position when it is not
      working capital;
    - a value<->value swap (e.g. USDC<->WETH) tracks nothing.

    ``out_qty`` may be None (an aggregator that did not report a human output);
    without a size we cannot track the acquired position's close math, so that
    leg is dropped rather than written with an unknown quantity.

    071 W3: ``cost_usd`` is the USD value the rail recorded for the swap — the
    cost of what was bought AND the proceeds of what was sold (one swap, one
    valuation). ``None`` (or a non-positive figure) is an UNKNOWN value, never
    $0. ``qty_source`` identifies simulated balance deltas (``"simulation"``),
    legacy unverified events (``"receipt"``), or quote estimates (the default).
    """
    value = (float(cost_usd) if cost_usd is not None and float(cost_usd) > 0
             else None)
    deltas: List[PositionDelta] = []
    if (out_qty is not None and out_qty > 0
            and not _is_value_asset(chain, token_out, is_native=False)):
        deltas.append(PositionDelta(
            chain=chain, address=_norm(token_out),
            symbol=out_symbol or (token_out or "?"),
            qty=float(out_qty), cost_usd=value, qty_source=qty_source))
    if (in_qty and in_qty > 0
            and not _is_value_asset(chain, token_in, is_native=in_native)):
        deltas.append(PositionDelta(
            chain=chain, address=_norm(token_in),
            symbol=in_symbol or (token_in or "?"),
            qty=-float(in_qty), cost_usd=None, proceeds_usd=value,
            qty_source=qty_source))
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


def _account_key(account: Optional[str]) -> str:
    return str(account or "").strip().lower()


def _migrate(conn) -> None:
    """Pre-050 stores have PK ``(user_id, chain, address)`` and no holder. SQLite
    cannot alter a primary key, so rebuild once: every existing row becomes the
    treasury's (``account = ''``), ``origin = 'trade'``.

    ⚠️ Idempotent under concurrency: the column check is repeated INSIDE the
    write lock — a second process that saw the old schema before the first one
    finished would otherwise rename the NEW table and rebuild it again.
    ⚠️ No ``executescript`` in here: it COMMITs the open transaction first."""
    def _cols():
        return [r[1] for r in conn.execute("PRAGMA table_info(open_positions)").fetchall()]
    cols = _cols()
    if not cols:
        return
    if "account" in cols:
        _add_status_columns(conn, _cols)
        _nullable_basis(conn, _cols)
        _add_columns(conn, _cols, _BOOK_COLUMNS)
        _zero_basis_is_unknown(conn)
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = _cols()
        if cols and "account" not in cols:
            conn.execute("ALTER TABLE open_positions RENAME TO open_positions_pre050")
            conn.execute("DROP INDEX IF EXISTS idx_open_positions_addr")
            conn.execute(_TABLE)
            conn.execute(
                "INSERT INTO open_positions (user_id, chain, account, address, symbol, qty, "
                "entry_usd, entry_ts, updated_ts, origin) SELECT user_id, chain, '', address, "
                "symbol, qty, entry_usd, entry_ts, updated_ts, 'trade' FROM open_positions_pre050")
            conn.execute("DROP TABLE open_positions_pre050")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _add_status_columns(conn, cols_fn) -> None:
    """W0: add the lifecycle columns to a pre-W0 store (every row = ``open``).
    Idempotent under concurrency: re-checked inside the write lock."""
    _add_columns(conn, cols_fn, _STATUS_COLUMNS)


def _add_columns(conn, cols_fn, columns) -> None:
    """Add any missing ``(name, decl)`` column. Re-checked inside the write lock."""
    if all(name in cols_fn() for name, _ in columns):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = cols_fn()
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE open_positions ADD COLUMN {name} {decl}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _basis_not_null(conn) -> bool:
    for r in conn.execute("PRAGMA table_info(open_positions)").fetchall():
        if r[1] == "entry_usd":
            return bool(r[3])
    return False


def _nullable_basis(conn, cols_fn) -> None:
    """071 W3 (TM P1-11): ``entry_usd`` was ``NOT NULL``, so an unknown basis
    was stored as ``0`` and the console showed the full value as profit.
    SQLite cannot drop a NOT NULL constraint, so rebuild once, copying every
    column the old table has. An ``inherited`` row's ``0`` was never a basis
    (``seed_inherited`` wrote it as a placeholder) and becomes NULL.

    ⚠️ Same rules as the pre-050 rebuild: re-checked INSIDE the write lock (a
    second process must not rebuild the new table), no ``executescript``."""
    if not _basis_not_null(conn):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _basis_not_null(conn):
            old = cols_fn()
            conn.execute("ALTER TABLE open_positions RENAME TO open_positions_pre071")
            conn.execute("DROP INDEX IF EXISTS idx_open_positions_addr")
            conn.execute(_TABLE)
            new = [r[1] for r in conn.execute(
                "PRAGMA table_info(open_positions)").fetchall()]
            common = ", ".join(c for c in old if c in new)
            conn.execute(f"INSERT INTO open_positions ({common}) "
                         f"SELECT {common} FROM open_positions_pre071")
            conn.execute("DROP TABLE open_positions_pre071")
            # Pre-071 `apply_delta` stored `cost_usd or 0.0`: EVERY unvalued
            # row's 0 is "unknown", not a free acquisition (071 review).
            conn.execute("UPDATE open_positions SET entry_usd=NULL WHERE entry_usd=0")
            conn.execute(_INDEX)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _zero_basis_is_unknown(conn) -> None:
    """071 review: a store migrated before this fix kept pre-071 ``0.0`` bases
    (an unvalued swap), which ``positions`` showed as a $0 basis and the next
    sell booked as a KNOWN gain. No real acquisition costs exactly $0, so a 0
    basis is unknown. Idempotent; a no-op once clean."""
    try:
        if conn.execute("SELECT 1 FROM open_positions WHERE entry_usd=0 LIMIT 1").fetchone():
            conn.execute("UPDATE open_positions SET entry_usd=NULL WHERE entry_usd=0")
    except Exception:
        logger.debug("open_positions: zero-basis cleanup skipped", exc_info=True)


def _init(db_path: str) -> None:
    from core.sqlite_util import wal_connect
    conn = wal_connect(db_path)
    try:
        conn.isolation_level = None
        _migrate(conn)
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def _ensure_readable(db_path: str) -> str:
    """A READ never creates the store, but it may upgrade an EXISTING one — or
    every pre-050 book would read as empty until the next trade.

    Returns the SELECT fragment for the lifecycle columns: a reader that cannot
    upgrade a pre-W0 store (read-only unit) reads every row as ``open``."""
    from core.sqlite_util import wal_connect
    conn = wal_connect(db_path)
    try:
        conn.isolation_level = None
        try:
            _migrate(conn)
        except Exception:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(open_positions)").fetchall()]
            if "account" not in cols:
                raise
            logger.debug("open positions: lifecycle columns not added (read-only?)",
                         exc_info=True)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(open_positions)").fetchall()]
    finally:
        conn.close()
    # A column the reader could not add reads as its default (a read-only unit).
    defaults = (("status", "'open'"), ("status_reason", "''"), ("qty_source", "''"),
                ("high_water_usd", "NULL"), ("high_water_ts", "NULL"))
    return ", ".join(name if name in cols else f"{default} AS {name}"
                     for name, default in defaults)


def _entry_from_row(r) -> PositionEntry:
    """One store row -> :class:`PositionEntry`. A NULL basis stays None."""
    keys = r.keys()
    entry = r["entry_usd"]
    hw = r["high_water_usd"] if "high_water_usd" in keys else None
    hw_ts = r["high_water_ts"] if "high_water_ts" in keys else None
    return PositionEntry(
        chain=r["chain"], address=r["address"], symbol=r["symbol"],
        qty=float(r["qty"]), entry_usd=(None if entry is None else float(entry)),
        entry_ts=float(r["entry_ts"]), account=r["account"], origin=r["origin"],
        status=r["status"] or STATUS_OPEN, status_reason=r["status_reason"] or "",
        qty_source=(r["qty_source"] if "qty_source" in keys else "") or "",
        high_water_usd=(None if hw is None else float(hw)),
        high_water_ts=(None if hw_ts is None else float(hw_ts)))


def apply_delta(user_id: str, delta: PositionDelta, *,
                db_path: Optional[str] = None,
                now: Optional[float] = None) -> None:
    """Apply one acquire/dispose leg to the tenant's open position. Fail-open.

    Never raises into the caller: a money verb's recorded spend must stand even
    if this bookkeeping write fails.

    071 W3: an UNKNOWN cost (``cost_usd is None``) makes the basis unknown
    (NULL) — a basis that is part known, part unknown is not a number. A
    dispose books what it realized (proceeds − the average-cost basis of the
    size sold) into ``position_realized``; an unknown basis or proceeds counts
    an unknown leg instead of a $0 one.
    """
    uid = str(user_id or "").strip()
    if not uid:
        # A position with no owner is a row no surface can tenant-scope back.
        return
    if not delta.address:
        return
    acct = _account_key(delta.account)
    try:
        from core.sqlite_util import execute_retry
        path = db_path or open_positions_db_path()
        _init(path)
        ts = time.time() if now is None else now
        row = execute_retry(
            path,
            "SELECT qty, entry_usd, entry_ts, status, status_reason, status_ts, "
            "qty_source, high_water_usd, high_water_ts "
            "FROM open_positions "
            "WHERE user_id=? AND chain=? AND account=? AND address=?",
            (uid, delta.chain, acct, delta.address), fetch="one")
        cur_qty = float(row["qty"]) if row else 0.0
        # None = unknown basis. A new row starts from a known $0 of nothing.
        cur_entry: Optional[float] = (
            (None if row["entry_usd"] is None else float(row["entry_usd"]))
            if row else 0.0)
        cur_entry_ts = float(row["entry_ts"]) if row else ts
        source = str(delta.qty_source or "")

        if delta.qty > 0:  # acquire (open / increase)
            new_qty = cur_qty + delta.qty
            new_entry = (None if cur_entry is None or delta.cost_usd is None
                         else cur_entry + float(delta.cost_usd))
            # Adding a simulated trade cannot upgrade unverified legacy size.
            if row:
                quality = ("", "quote", "receipt", "simulation", "inherited")
                sources = (row["qty_source"] or "", source)
                source = min(sources, key=lambda s: quality.index(s) if s in quality else 0)
            # W0: a later buy never silently lifts a quarantine — the question
            # of which contract is real has not changed.
            execute_retry(
                path,
                "INSERT OR REPLACE INTO open_positions "
                "(user_id, chain, account, address, symbol, qty, entry_usd, entry_ts, "
                "updated_ts, status, status_reason, status_ts, qty_source, "
                "high_water_usd, high_water_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, delta.chain, acct, delta.address, delta.symbol, new_qty,
                 new_entry, cur_entry_ts, ts,
                 (row["status"] if row else STATUS_OPEN) or STATUS_OPEN,
                 (row["status_reason"] if row else "") or "",
                 row["status_ts"] if row else None,
                 source,
                 row["high_water_usd"] if row else None,
                 row["high_water_ts"] if row else None))
            return

        # dispose (reduce / close)
        if not row or cur_qty <= 0:
            return  # nothing held — never open a negative/short position
        sell = -delta.qty
        sold = min(sell, cur_qty)
        _book_realized(path, uid, delta, acct, sold=sold, sell=sell,
                       cur_qty=cur_qty, cur_entry=cur_entry, ts=ts)
        if sell >= cur_qty - abs(cur_qty) * _CLOSE_EPS:
            execute_retry(
                path,
                "DELETE FROM open_positions "
                "WHERE user_id=? AND chain=? AND account=? AND address=?",
                (uid, delta.chain, acct, delta.address))
            return
        remaining = cur_qty - sell
        # Reduce the cost basis in the SAME proportion as the size, so the
        # entry_usd left still describes the size left (average cost).
        new_entry = None if cur_entry is None else cur_entry * (remaining / cur_qty)
        execute_retry(
            path,
            "UPDATE open_positions SET qty=?, entry_usd=?, updated_ts=? "
            "WHERE user_id=? AND chain=? AND account=? AND address=?",
            (remaining, new_entry, ts, uid, delta.chain, acct, delta.address))
    except Exception:
        logger.debug("open-position write skipped (fail-open)", exc_info=True)


def _book_realized(path: str, uid: str, delta: PositionDelta, acct: str, *,
                   sold: float, sell: float, cur_qty: float,
                   cur_entry: Optional[float], ts: float) -> None:
    """Add one dispose leg to ``position_realized`` (average cost).

    The basis of the size sold is ``entry_usd × sold / qty``; the proceeds of
    that size are the leg's proceeds × ``sold / sell`` (an oversell's excess was
    never tracked). Either side unknown → an unknown leg, never a $0 one."""
    from core.sqlite_util import execute_retry
    known = (cur_entry is not None and delta.proceeds_usd is not None
             and sell > 0 and cur_qty > 0)
    pnl = 0.0
    if known:
        basis_sold = cur_entry * (sold / cur_qty)
        proceeds = float(delta.proceeds_usd) * (sold / sell)
        pnl = proceeds - basis_sold
    execute_retry(
        path,
        "INSERT INTO position_realized (user_id, chain, account, address, symbol, "
        "realized_usd, known_legs, unknown_legs, sold_qty, updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, chain, account, address) DO UPDATE SET "
        "realized_usd = realized_usd + excluded.realized_usd, "
        "known_legs = known_legs + excluded.known_legs, "
        "unknown_legs = unknown_legs + excluded.unknown_legs, "
        "sold_qty = sold_qty + excluded.sold_qty, "
        "symbol = COALESCE(excluded.symbol, symbol), "
        "updated_ts = excluded.updated_ts",
        (uid, delta.chain, acct, delta.address, delta.symbol, pnl,
         1 if known else 0, 0 if known else 1, sold, ts))


def apply_deltas(user_id: str, deltas: List[PositionDelta], *,
                 db_path: Optional[str] = None,
                 now: Optional[float] = None) -> None:
    """Apply every leg of a trade. Fail-open per leg."""
    for d in deltas or []:
        apply_delta(user_id, d, db_path=db_path, now=now)


def get_position(user_id: str, chain: str, address: str, *,
                 db_path: Optional[str] = None,
                 account: str = TREASURY_ACCOUNT) -> Optional[PositionEntry]:
    """One tenant-scoped position by ``(chain, account, address)``, or None.
    Read-only — never creates the store."""
    uid = str(user_id or "").strip()
    if not uid:
        return None
    path = db_path or open_positions_db_path()
    if not os.path.isfile(path):
        return None
    try:
        from core.sqlite_util import execute_retry
        lifecycle = _ensure_readable(path)
        row = execute_retry(
            path,
            "SELECT chain, account, address, symbol, qty, entry_usd, entry_ts, origin, "
            f"{lifecycle} "
            "FROM open_positions WHERE user_id=? AND chain=? AND account=? AND address=?",
            (uid, chain, _account_key(account), _norm(address)), fetch="one")
    except Exception:
        logger.warning("open-position read failed for %s", uid, exc_info=True)
        return None
    if not row:
        return None
    return _entry_from_row(row)


def entries_for(user_id: str, *, data_dir: Optional[str] = None,
                db_path: Optional[str] = None,
                account: str = TREASURY_ACCOUNT,
                strict: bool = False) -> Dict[str, PositionEntry]:
    """Every open position one holder of a tenant holds, keyed by NORMALIZED address.

    ``account`` defaults to the treasury, so the Money Book reads exactly what it
    read before 050; a token-bound account's book is asked for by its address.

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
    # 068 B1 (round 2): only a genuinely absent file is an empty book. isfile()
    # also answers False when a parent directory denies search, which on the
    # spend path read an unreadable store as "no tracked positions".
    try:
        os.stat(path)
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        logger.warning("open-position store unreachable: %s", path, exc_info=True)
        return {}
    try:
        from core.sqlite_util import execute_retry
        lifecycle = _ensure_readable(path)
        rows = execute_retry(
            path,
            "SELECT chain, account, address, symbol, qty, entry_usd, entry_ts, origin, "
            f"{lifecycle} "
            "FROM open_positions WHERE user_id=? AND account=? ORDER BY updated_ts ASC",
            (uid, _account_key(account)), fetch="all") or []
    except Exception:
        # 068 B1: the spend path asks strict=True — an existing store it cannot
        # read is not an empty book, and reading it as empty drops a collision.
        if strict:
            raise
        logger.warning("open-position read failed for %s", uid, exc_info=True)
        return {}
    out: Dict[str, PositionEntry] = {}
    for r in rows:
        # A later-updated row for the same address wins (ORDER BY updated_ts ASC).
        out[_norm(r["address"])] = _entry_from_row(r)
    return out


def positions_for(user_id: str, *, chain: Optional[str] = None,
                  account: str = TREASURY_ACCOUNT,
                  db_path: Optional[str] = None) -> List[PositionEntry]:
    """071 W3: every tracked row of one holder, chain KEPT (``entries_for``
    keys by address and so folds two chains' rows of one address together).

    Read-only; never creates the store. ⚠️ STRICT: an existing store that
    cannot be read RAISES — the ``positions`` verb must say "I could not read
    the book", never "no positions"."""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    path = db_path or open_positions_db_path()
    try:
        os.stat(path)
    except FileNotFoundError:
        return []
    from core.sqlite_util import execute_retry
    lifecycle = _ensure_readable(path)
    sql = ("SELECT chain, account, address, symbol, qty, entry_usd, entry_ts, origin, "
           f"{lifecycle} FROM open_positions WHERE user_id=? AND account=?")
    args: tuple = (uid, _account_key(account))
    if chain:
        sql += " AND lower(chain)=?"
        args += (str(chain).strip().lower(),)
    rows = execute_retry(path, sql + " ORDER BY chain, entry_ts ASC", args,
                         fetch="all") or []
    return [_entry_from_row(r) for r in rows]


def realized_for(user_id: str, *, chain: Optional[str] = None,
                 account: str = TREASURY_ACCOUNT,
                 db_path: Optional[str] = None) -> List[RealizedEntry]:
    """071 W3: the realized P&L rows of one holder. STRICT like
    :func:`positions_for`; a store older than the realized table has none."""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    path = db_path or open_positions_db_path()
    try:
        os.stat(path)
    except FileNotFoundError:
        return []
    from core.sqlite_util import execute_retry
    have = execute_retry(
        path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_realized'",
        (), fetch="one")
    if not have:
        return []  # written before 071 W3: no dispose was ever booked here
    sql = ("SELECT chain, account, address, symbol, realized_usd, known_legs, "
           "unknown_legs, sold_qty, updated_ts FROM position_realized "
           "WHERE user_id=? AND account=?")
    args: tuple = (uid, _account_key(account))
    if chain:
        sql += " AND lower(chain)=?"
        args += (str(chain).strip().lower(),)
    rows = execute_retry(path, sql + " ORDER BY chain, updated_ts ASC", args,
                         fetch="all") or []
    return [RealizedEntry(
        chain=r["chain"], address=r["address"], symbol=r["symbol"], account=r["account"],
        realized_usd=float(r["realized_usd"] or 0.0), known_legs=int(r["known_legs"] or 0),
        unknown_legs=int(r["unknown_legs"] or 0), sold_qty=float(r["sold_qty"] or 0.0),
        updated_ts=float(r["updated_ts"])) for r in rows]


def observe_price(user_id: str, chain: str, address: str, price_usd: float, *,
                  account: str = TREASURY_ACCOUNT, db_path: Optional[str] = None,
                  now: Optional[float] = None) -> Optional[float]:
    """071 W3: raise the row's high-water mark to ``price_usd`` when it is
    higher. Returns the high-water mark after the update (None = no row).

    Only updates an EXISTING row — never creates the store or a position.
    Fail-open: a failed write returns None and changes nothing."""
    uid = str(user_id or "").strip()
    if not uid or not address or price_usd is None or price_usd <= 0:
        return None
    path = db_path or open_positions_db_path()
    if not os.path.isfile(path):
        return None
    try:
        from core.sqlite_util import execute_retry
        _init(path)
        ts = time.time() if now is None else now
        key = (uid, str(chain or "").strip().lower(), _account_key(account), _norm(address))
        execute_retry(
            path,
            "UPDATE open_positions SET high_water_usd=?, high_water_ts=? "
            "WHERE user_id=? AND lower(chain)=? AND account=? AND address=? "
            "AND (high_water_usd IS NULL OR high_water_usd < ?)",
            (float(price_usd), ts) + key + (float(price_usd),))
        row = execute_retry(
            path,
            "SELECT high_water_usd FROM open_positions "
            "WHERE user_id=? AND lower(chain)=? AND account=? AND address=?",
            key, fetch="one")
        if not row or row["high_water_usd"] is None:
            return None
        return float(row["high_water_usd"])
    except Exception:
        logger.debug("high-water update skipped (fail-open)", exc_info=True)
        return None


def set_status(user_id: str, chain: str, address: str, status: str, *,
               reason: str = "", account: str = TREASURY_ACCOUNT,
               db_path: Optional[str] = None, now: Optional[float] = None) -> bool:
    """Set one position's lifecycle status (W0). Returns True when a row changed.

    The cost basis and size are untouched: a quarantined look-alike still
    carries what it cost, so the loss stays visible. Never creates the store.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown position status {status!r}; one of {STATUSES}")
    uid = str(user_id or "").strip()
    if not uid or not address:
        return False
    path = db_path or open_positions_db_path()
    if not os.path.isfile(path):
        return False
    from core.sqlite_util import execute_retry
    _init(path)
    ts = time.time() if now is None else now
    changed = execute_retry(
        path,
        "UPDATE open_positions SET status=?, status_reason=?, status_ts=? "
        "WHERE user_id=? AND lower(chain)=? AND account=? AND address=? AND status<>?",
        (status, str(reason or "")[:300], ts, uid, str(chain or "").strip().lower(),
         _account_key(account), _norm(address), status))
    return bool(changed)


def seed_inherited(user_id: str, *, chain: str, account: str, address: str, symbol: Optional[str],
                   qty: float, db_path: Optional[str] = None, now: Optional[float] = None) -> bool:
    """Record a position a token-bound account ALREADY held when it was adopted (050 §6, adopt).

    No cost basis exists for it — the seller's entry is not ours to claim — so
    ``entry_usd`` is NULL (unknown, 071 W3 — it was ``0`` before) and
    ``origin = 'inherited'``; the book reads it NO_LEDGER.
    Refuses the treasury (``account`` must be a token-bound account) and never
    overwrites a row the rail already tracks. Returns True when a row was written.
    """
    uid = str(user_id or "").strip()
    acct = _account_key(account)
    if not uid or not acct or not address or not (qty and qty > 0):
        return False
    from core.sqlite_util import execute_retry
    path = db_path or open_positions_db_path()
    _init(path)
    ts = time.time() if now is None else now
    cur = execute_retry(
        path, "SELECT 1 FROM open_positions WHERE user_id=? AND chain=? AND account=? AND address=?",
        (uid, chain, acct, _norm(address)), fetch="one")
    if cur:
        return False
    execute_retry(
        path,
        "INSERT INTO open_positions (user_id, chain, account, address, symbol, qty, entry_usd, "
        "entry_ts, updated_ts, origin, qty_source) "
        "VALUES (?,?,?,?,?,?,NULL,?,?,'inherited','inherited')",
        (uid, chain, acct, _norm(address), symbol, float(qty), ts, ts))
    return True
