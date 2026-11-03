"""Deposit-sweep state on ``crypto_payments`` (098 close-out MO-01).

``crypto_payments.sweep_state`` makes the treasury sweep of a deposit group an
explicit machine:

    unswept ──▶ sweeping ──▶ swept           (local signer: a receipt proves it)
       ▲           │    └──▶ swept_unproven  (remote signer: a tx hash, no receipt)
       │           ├──▶ sweep_failed         (the receipt says it reverted)
       └───────────┘    (nothing was broadcast: released)
    sweep_failed ──▶ sweeping                (only inside a later deposit's group)

The claim (``→ sweeping``) is ONE guarded write over the whole pinned group, so
two sweepers never both broadcast for it. A broadcast whose outcome is unknown
stays ``sweeping``: the reaper (:func:`reap_stuck_sweeps`) tells the owner ONCE
and never resends; the owner decides it (:func:`resolve_stuck_sweep`,
``polyrob owner deposit-resolve``). ``swept_unproven`` is never booked on the deposit
balance mark (there is no receipt to book). :func:`deposit_report` is the
read-only owner reconcile view (``polyrob owner deposits``).
"""
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

DEPOSIT_SWEEP = StateMachine(
    name="deposit_sweep",
    states=frozenset({"unswept", "sweeping", "swept", "swept_unproven", "sweep_failed"}),
    transitions={
        "unswept": frozenset({"sweeping"}),
        "sweeping": frozenset({"swept", "swept_unproven", "sweep_failed", "unswept"}),
        "sweep_failed": frozenset({"sweeping"}),
    },
    terminal=frozenset({"swept", "swept_unproven"}),
    initial=frozenset({"unswept", "swept"}),
)

#: A sweep still ``sweeping`` after this long goes to the owner (once).
SWEEPING_MAX_AGE_SEC = 3600

_COLUMNS = (("sweep_state", "TEXT NOT NULL DEFAULT 'unswept'"),
            ("sweep_started_at", "INTEGER"),
            ("sweep_escalated_at", "INTEGER"))


async def ensure_sweep_schema(db) -> None:
    """Add the sweep-state columns to ``crypto_payments`` (idempotent). A row
    already swept before this column existed reads ``swept``. Shared by the
    sweeper, the owner report and migration v1.15.0. No table = nothing to do."""
    try:
        cols = await db.fetch_all("PRAGMA table_info(crypto_payments)")
    except Exception:
        cols = None
    if not cols:
        return
    have = {c["name"] for c in cols}
    for name, sql_type in _COLUMNS:
        if name not in have:
            try:
                await db.execute(f"ALTER TABLE crypto_payments ADD COLUMN {name} {sql_type}")
            except Exception as e:  # a concurrent add (the only error swallowed)
                if "duplicate column" not in str(e).lower():
                    raise
    # ⚠️ Not gated on "the column was just added": the ALTER and this UPDATE are
    # two statements, and a crash between them left legacy swept rows
    # `unswept` for ever (098 review). New code sets swept_at and sweep_state
    # together, so the predicate is empty after the backfill — the read keeps
    # the owner report from writing.
    stale = await db.fetch_one("SELECT 1 AS x FROM crypto_payments WHERE swept_at IS NOT NULL "
                               "AND sweep_state = 'unswept' LIMIT 1")
    if stale:
        await db.execute("UPDATE crypto_payments SET sweep_state = 'swept' "
                         "WHERE swept_at IS NOT NULL AND sweep_state = 'unswept'")


def _rowcount(res) -> int:
    return res if isinstance(res, int) else int(getattr(res, "rowcount", 0) or 0)


async def move_group(db, ids: Iterable[int], to: str, from_states: Iterable[str], *,
                     started_at: Optional[int] = None, extra_set: str = "",
                     extra_params: tuple = ()) -> int:
    """``from_states → to`` for every row of a pinned group in ONE guarded
    UPDATE. ``started_at`` pins the claim that owns a ``sweeping`` group.
    Returns the row count (the caller compares it with ``len(ids)``)."""
    ids = tuple(int(i) for i in ids)
    froms = DEPOSIT_SWEEP.require_sources(to, tuple(from_states), ctx={"ids": ids})
    if not ids or not froms:
        return 0
    sets = "sweep_state = ?" + (", " + extra_set if extra_set else "")
    params: List[Any] = [to, *extra_params]
    if to == "sweeping":
        sets += ", sweep_started_at = ?, sweep_escalated_at = NULL"
        params.append(int(started_at if started_at is not None else time.time()))
    where = (f"id IN ({','.join('?' * len(ids))}) "
             f"AND sweep_state IN ({','.join('?' * len(froms))})")
    params += [*ids, *froms]
    if started_at is not None and to != "sweeping":
        where += " AND sweep_started_at = ?"
        params.append(int(started_at))
    return _rowcount(await db.execute(
        f"UPDATE crypto_payments SET {sets} WHERE {where}", tuple(params)))


async def claim_group(db, ids: Iterable[int], started_at: int) -> bool:
    """All-or-nothing claim of a pinned deposit group (``unswept`` /
    ``sweep_failed`` → ``sweeping``). A partial claim (another sweeper holds a
    row) is given back at once and nothing is broadcast."""
    ids = tuple(int(i) for i in ids)
    n = await move_group(db, ids, "sweeping", ("unswept", "sweep_failed"),
                         started_at=started_at)
    if n == len(ids):
        return True
    if n:
        await move_group(db, ids, "unswept", ("sweeping",), started_at=started_at)
    return False


def _emit(kind: str, *, user_id: str, attrs: dict) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="treasury_sweeper", user_id=user_id, attrs=attrs)
    except Exception:
        logger.debug("deposit sweep: event %s not recorded", kind, exc_info=True)


def emit_sweep_failed(deposit: Dict[str, Any], tx_hash: Optional[str]) -> None:
    from core.event_kinds import PAYMENT_DEPOSIT_SWEEP_FAILED
    _emit(PAYMENT_DEPOSIT_SWEEP_FAILED, user_id=str(deposit.get("user_id") or ""),
          attrs={"deposit_ids": list(deposit.get("sweep_deposit_ids") or ()),
                 "chain": deposit.get("chain"), "token": deposit.get("token_symbol"),
                 "tx_hash": tx_hash or ""})


async def reap_stuck_sweeps(db, *, max_age_seconds: int = SWEEPING_MAX_AGE_SEC,
                            now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every ``sweeping`` row older than ``max_age_seconds`` is reported to the
    owner ONCE (``payment_deposit_sweep_stuck``). The row stays ``sweeping``:
    the broadcast outcome is unknown, so nothing is resent and nothing is
    released — the owner decides it (``polyrob owner deposit-resolve``)."""
    from core.event_kinds import PAYMENT_DEPOSIT_SWEEP_STUCK
    await ensure_sweep_schema(db)
    t = int(time.time() if now is None else now)
    rows = await db.fetch_all(
        "SELECT id, user_id, chain, token_symbol, amount_usd, sweep_started_at "
        "FROM crypto_payments WHERE sweep_state = 'sweeping' AND sweep_escalated_at IS NULL "
        "AND COALESCE(sweep_started_at, 0) < ?", (t - int(max_age_seconds),))
    out = []
    for row in rows or []:
        row = dict(row)
        res = await db.execute(
            "UPDATE crypto_payments SET sweep_escalated_at = ? WHERE id = ? "
            "AND sweep_state = 'sweeping' AND sweep_escalated_at IS NULL", (t, row["id"]))
        if _rowcount(res) != 1:
            continue
        out.append(row)
        logger.error("deposit sweep %s is still in flight after %ss — the outcome is "
                     "unknown; it is NOT resent. Decide it: polyrob owner deposit-resolve <id>",
                     row["id"], max_age_seconds)
        _emit(PAYMENT_DEPOSIT_SWEEP_STUCK, user_id=str(row.get("user_id") or ""),
              attrs={"deposit_id": row["id"], "chain": row.get("chain"),
                     "token": row.get("token_symbol"), "amount_usd": row.get("amount_usd"),
                     "since": row.get("sweep_started_at")})
    return out


async def note_sweep_tx(db, ids: Iterable[int], started_at: int, tx_hash: str) -> int:
    """098 review (O-02): a broadcast whose outcome is unknown keeps its tx hash
    on the group (state unchanged), so the owner can check that one tx."""
    ids = tuple(int(i) for i in ids)
    if not ids or not tx_hash:
        return 0
    return _rowcount(await db.execute(
        f"UPDATE crypto_payments SET sweep_tx_hash = ? WHERE id IN ({','.join('?' * len(ids))}) "
        "AND sweep_state = 'sweeping' AND sweep_started_at = ?",
        (tx_hash, *ids, int(started_at))))


async def resolve_stuck_sweep(db, deposit_id: int, *, sent: bool,
                              tx_hash: Optional[str] = None) -> int:
    """098 review (O-02): the owner's decision on a ``sweeping`` group whose
    outcome no reaper can know. ``sent=False`` (the owner checked: nothing left
    the deposit address) releases the group to ``unswept`` — the next tick sweeps
    it again. ``sent=True`` records ``swept_unproven`` with the tx the owner saw
    (never booked on the balance mark). The group is every ``sweeping`` row of
    the same claim (``sweep_started_at``). Returns the rows moved (0 = not
    sweeping any more, or unknown id)."""
    await ensure_sweep_schema(db)
    row = await db.fetch_one(
        "SELECT user_id, chain, token_symbol, sweep_started_at, sweep_tx_hash FROM crypto_payments "
        "WHERE id = ? AND sweep_state = 'sweeping'", (int(deposit_id),))
    if not row:
        return 0
    row = dict(row)
    rows = await db.fetch_all(
        "SELECT id FROM crypto_payments WHERE sweep_state = 'sweeping' AND user_id = ? "
        "AND chain = ? AND token_symbol = ? AND sweep_started_at IS ?",
        (row["user_id"], row["chain"], row["token_symbol"], row["sweep_started_at"]))
    ids = tuple(int(r["id"]) for r in rows or [])
    if not sent:
        return await move_group(db, ids, "unswept", ("sweeping",),
                                started_at=row["sweep_started_at"])
    tx = tx_hash or row.get("sweep_tx_hash")
    if not tx:
        raise ValueError("a sent sweep needs its tx hash (--tx)")
    return await move_group(db, ids, "swept_unproven", ("sweeping",),
                            started_at=row["sweep_started_at"],
                            extra_set="swept_at = datetime('now'), sweep_tx_hash = ?",
                            extra_params=(tx,))


async def deposit_report(db, *, user_id: Optional[str] = None,
                         onchain=None) -> List[Dict[str, Any]]:
    """The owner's read-only reconcile view, one entry per (user, chain, token):
    credited deposits by sweep state, the deposit monitor's balance mark (and
    the block it is as of), and — when ``onchain(user_id, chain, token)`` is
    given — the address balance on chain now (None = unknown). Nothing moves."""
    await ensure_sweep_schema(db)
    try:
        cols = await db.fetch_all("PRAGMA table_info(crypto_payments)")
    except Exception:
        cols = None
    if not cols:
        return []
    where, params = ("WHERE user_id = ?", (user_id,)) if user_id else ("", ())
    rows = await db.fetch_all(
        "SELECT user_id, chain, token_symbol, sweep_state, COUNT(*) AS n, "
        "COALESCE(SUM(amount_usd), 0) AS usd FROM crypto_payments "
        f"{where} GROUP BY user_id, chain, token_symbol, sweep_state", params)
    marks: Dict[tuple, dict] = {}
    try:
        for m in await db.fetch_all("SELECT user_id, chain, token_symbol, balance, as_of_block "
                                    "FROM deposit_balance_marks") or []:
            marks[(m["user_id"], m["chain"], m["token_symbol"])] = dict(m)
    except Exception:
        pass   # no marks table yet: the monitor never credited
    out: Dict[tuple, Dict[str, Any]] = {}
    for r in rows or []:
        key = (r["user_id"], r["chain"], r["token_symbol"])
        item = out.setdefault(key, {"user_id": key[0], "chain": key[1], "token": key[2],
                                    "credited_usd": 0.0, "states": {}})
        item["states"][r["sweep_state"]] = {"count": int(r["n"]),
                                             "usd": round(float(r["usd"]), 6)}
        item["credited_usd"] = round(item["credited_usd"] + float(r["usd"]), 6)
    for key, item in out.items():
        mark = marks.get(key)
        item["mark_balance"] = mark["balance"] if mark else None
        item["mark_as_of_block"] = mark["as_of_block"] if mark else None
        item["onchain_balance"] = None
        if onchain is not None:
            try:
                item["onchain_balance"] = await onchain(*key)
            except Exception:
                item["onchain_balance"] = None
        item["needs_owner"] = any(s in item["states"] for s in
                                  ("sweeping", "sweep_failed", "swept_unproven"))
    return [out[k] for k in sorted(out)]
