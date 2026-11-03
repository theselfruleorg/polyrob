"""Billing-failure resolution as an explicit machine (098 W1 MO-07, MO-08).

``billing_failures.status`` used to move ``pending → resolved`` with one
unguarded write AFTER the money step, so two admin requests both charged the
user. Now:

    pending ─┐                    ┌─▶ charged
             ├─▶ resolving ───────┼─▶ written_off
    hold_unresolved ─┘   │        └─▶ refunded
             ▲           └─▶ (back to where it came from) on a failed money step

The claim (``→ resolving``) is ONE guarded write, so exactly one admin request
reaches the money step. A non-reserved charge carries the reason
``billing-failure:<id>``, which a UNIQUE partial index makes chargeable once.
A ``resolving`` row older than :data:`RESOLVING_MAX_AGE_SEC` is finished by
:func:`reap_resolving` from the credit ledger (S6).

``hold_unresolved`` (MO-08): a credit reservation whose call raised before it
was settled. It is the platform's open question, not the user's debt, so it
does not block new reservations; the admin resolves it, no timer releases it.
"""
import logging
import time
from typing import Any, Dict, Optional

from core.state.machine import StateMachine
from core.state.sql import acas_transition

logger = logging.getLogger(__name__)

KINDS = ("charged", "written_off", "refunded")
RESOLVING_MAX_AGE_SEC = 600
#: The credit-ledger reason a non-reserved admin charge carries.
REASON_PREFIX = "billing-failure:"

BILLING_FAILURE = StateMachine(
    name="billing_failure",
    states=frozenset({"pending", "hold_unresolved", "resolving", "charged",
                      "written_off", "refunded", "resolved"}),
    transitions={
        "pending": frozenset({"resolving"}),
        "hold_unresolved": frozenset({"resolving"}),
        "resolving": frozenset({"pending", "hold_unresolved", *KINDS}),
    },
    # `resolved`: a legacy row whose kind could not be read from its notes.
    terminal=frozenset({*KINDS, "resolved"}),
    initial=frozenset({"pending", "hold_unresolved", "resolved"}),
)

#: Statuses that block the user's new reservations (an unpaid completed call).
BLOCKING_SQL = ("status = 'pending' OR (status = 'resolving' "
                "AND COALESCE(resolution, '') != 'from:hold_unresolved')")

_COLUMNS = (("resolution", "TEXT"), ("resolving_by", "TEXT"), ("resolving_at", "INTEGER"))


class ResolveError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


async def ensure_columns(db, log=logger) -> None:
    """Add the resolution columns + the one-charge index, idempotently. Shared by
    ``AuthTables.create_tables`` and migration v1.13.0."""
    try:
        cols = await db.fetch_all("PRAGMA table_info(billing_failures)")
    except Exception:
        cols = None
    if cols:
        have = {c["name"] for c in cols}
        for name, sql_type in _COLUMNS:
            if name not in have:
                try:
                    await db.execute(f"ALTER TABLE billing_failures ADD COLUMN {name} {sql_type}")
                except Exception as e:
                    log.error("billing_failures: could not add %s (%s)", name, e)
    try:
        if await db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name='credit_transactions'"):
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_trans_billing_failure "
                "ON credit_transactions(reason) WHERE reason LIKE 'billing-failure:%'")
    except Exception as e:
        log.error("credit_transactions: one-charge index failed (%s)", e)


async def _move(db, fid: int, to: str, frm: str, **cols) -> bool:
    return await acas_transition(db, "billing_failures", key="id = ?", key_params=(fid,),
                                 machine=BILLING_FAILURE, to=to, from_states=(frm,),
                                 set_cols=cols)


async def _charged_in_ledger(db, row) -> Optional[str]:
    """The kind the ledger proves for a row whose resolution was interrupted,
    or None when nothing moved."""
    rid = str(row.get("request_id") or "")
    if rid.startswith("llm-reserve:"):
        settled = await db.fetch_one(
            "SELECT amount FROM credit_transactions WHERE user_id = ? AND reason = ? "
            "AND transaction_type = 'usage'", (row["user_id"], rid + ":settled"))
        if settled is None:
            return None
        return "charged" if int(settled["amount"] or 0) != 0 else "written_off"
    hit = await db.fetch_one("SELECT 1 AS ok FROM credit_transactions WHERE reason = ?",
                             (f"{REASON_PREFIX}{row['id']}",))
    return "charged" if hit else None


def _origin(row) -> str:
    return ("hold_unresolved" if str(row.get("resolution") or "") == "from:hold_unresolved"
            else "pending")


async def reap_resolving(db, *, max_age_seconds: int = RESOLVING_MAX_AGE_SEC,
                         now: Optional[float] = None) -> int:
    """Finish every ``resolving`` row older than ``max_age_seconds`` from the
    credit ledger: the money moved → its kind; nothing moved → back to where it
    came from. Returns how many rows moved."""
    await ensure_columns(db)
    cutoff = int((time.time() if now is None else now) - max_age_seconds)
    rows = await db.fetch_all(
        "SELECT * FROM billing_failures WHERE status = 'resolving' AND "
        "COALESCE(resolving_at, 0) < ?", (cutoff,))
    moved = 0
    for row in rows or []:
        row = dict(row)
        kind = await _charged_in_ledger(db, row)
        if kind:
            ok = await _move(db, row["id"], kind, "resolving", resolution=kind,
                             resolved_at=_now_text(),
                             resolution_notes=f"{kind}: finished by the resolving reaper")
        else:
            ok = await _move(db, row["id"], _origin(row), "resolving", resolution=None,
                             resolving_by=None, resolving_at=None)
        moved += int(ok)
    return moved


#: A reservation older than this with no settlement is an open hold (MO-08).
HOLD_MAX_AGE_SEC = 3600


async def reap_open_holds(db, *, max_age_seconds: int = HOLD_MAX_AGE_SEC) -> int:
    """Record every credit reservation older than ``max_age_seconds`` that was
    never settled and has no failure row as ``hold_unresolved`` (MO-08): a call
    that raised or a process that died after reserving. The hold stays held —
    nothing is released by a timer; the row puts it in front of the admin."""
    try:
        if not await db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' "
                                  "AND name='credit_transactions'"):
            return 0
        cur = await db.execute(
            "INSERT OR IGNORE INTO billing_failures (user_id, session_id, request_id, "
            "credits_owed, api_cost_usd, model, created_at, status) "
            "SELECT ct.user_id, COALESCE(ct.session_id, ''), ct.reason, 0, 0, NULL, "
            "datetime('now'), 'hold_unresolved' FROM credit_transactions ct "
            "WHERE ct.transaction_type = 'reservation' AND ct.reason LIKE 'llm-reserve:%' "
            "AND ct.timestamp < datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM billing_failures bf WHERE bf.request_id = ct.reason)",
            (f"-{int(max_age_seconds)} seconds",))
    except Exception:
        logger.warning("credit holds: open-hold sweep failed", exc_info=True)
        return 0
    n = int(getattr(cur, "rowcount", 0) or 0)
    if n:
        logger.warning("credit holds: %d reservation(s) never settled — recorded as "
                       "hold_unresolved for the admin", n)
    return n


async def record_holds_unresolved(db, user_id: str, session_id, request_ids) -> int:
    """098 MO-08: record the given reservations as ``hold_unresolved`` at once
    (their call raised before settlement). Only a hold that is still a plain
    ``reservation`` row is recorded (a settled one is skipped); once per hold."""
    n = 0
    for rid in request_ids or ():
        try:
            cur = await db.execute(
                "INSERT OR IGNORE INTO billing_failures (user_id, session_id, request_id, "
                "credits_owed, api_cost_usd, model, created_at, status) "
                "SELECT ct.user_id, COALESCE(ct.session_id, ?, ''), ct.reason, 0, 0, NULL, "
                "datetime('now'), 'hold_unresolved' FROM credit_transactions ct "
                "WHERE ct.user_id = ? AND ct.reason = ? AND ct.transaction_type = 'reservation' "
                "AND NOT EXISTS (SELECT 1 FROM billing_failures bf WHERE bf.request_id = ct.reason)",
                (session_id, user_id, str(rid)))
            n += int(getattr(cur, "rowcount", 0) or 0)
        except Exception:
            logger.warning("credit holds: could not record an orphaned hold", exc_info=True)
    if n:
        logger.warning("credit holds: %d reservation(s) whose call raised — recorded as "
                       "hold_unresolved for the admin", n)
    return n


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


async def resolve(db, balance_mgr_fn, fid: int, resolution: str, *, notes: str = "",
                  admin_id: str = "unknown", amount: Optional[int] = None) -> Dict[str, Any]:
    """Resolve one failure. Raises :class:`ResolveError` (HTTP status + detail).
    ``balance_mgr_fn()`` returns the balance manager (only called when money moves)."""
    await ensure_columns(db)
    await reap_resolving(db)
    row = await db.fetch_one("SELECT * FROM billing_failures WHERE id = ?", (fid,))
    if not row:
        raise ResolveError(404, "Billing failure not found")
    row = dict(row)
    frm = row["status"]
    if frm not in ("pending", "hold_unresolved"):
        raise ResolveError(400 if frm != "resolving" else 409,
                           f"Failure already resolved with status: {frm}")
    reserved = str(row.get("request_id") or "").startswith("llm-reserve:")
    if not reserved and resolution == "refunded":
        raise ResolveError(400, "nothing was charged for this failure; use written_off")
    charge = int(row.get("credits_owed") or 0)
    if frm == "hold_unresolved" and resolution == "charged":
        if amount is None or int(amount) <= 0:
            raise ResolveError(400, "charging an unresolved hold needs an explicit amount")
        charge = int(amount)
    # The services the money step needs are fetched BEFORE the claim, so a
    # missing one refuses with the row untouched.
    mgr = balance_mgr_fn() if (reserved or resolution == "charged") else None
    if not await _move(db, fid, "resolving", frm, resolving_by=str(admin_id),
                       resolving_at=int(time.time()), resolution=f"from:{frm}"):
        raise ResolveError(409, "Billing failure is being resolved by another request")

    async def back(status: int, detail: str):
        await _move(db, fid, frm, "resolving", resolution=None, resolving_by=None,
                    resolving_at=None)
        raise ResolveError(status, detail)

    try:
        if reserved:
            from modules.credits.reservations import settle_reservation
            ok = await settle_reservation(mgr, row["user_id"], row["request_id"],
                                          charge if resolution == "charged" else 0)
            if not ok:
                await back(409, "Reservation could not be reconciled; no new charge was made")
        elif resolution == "charged":
            import sqlite3
            try:
                ok = await mgr.deduct_credits(
                    user_id=row["user_id"], amount=charge,
                    reason=f"{REASON_PREFIX}{fid}")
            except sqlite3.IntegrityError:
                ok = True  # the one-charge index: this failure was already charged
            if not ok:
                await back(400, "Failed to deduct credits - user may still have "
                                "insufficient balance")
    except ResolveError:
        raise
    except BaseException:
        # unknown outcome: the row stays `resolving`; the reaper reads the ledger
        raise
    if not await _move(db, fid, resolution, "resolving", resolution=resolution,
                       resolved_at=_now_text(),
                       resolution_notes=f"{resolution}: {notes} (by {admin_id})"):
        logger.warning("billing failure %s: resolving row changed under the resolver", fid)
    return {"success": True, "failure_id": fid, "resolution": resolution,
            "resolved_by": admin_id, "user_id": row["user_id"], "credits_owed": charge,
            "old_status": frm}
