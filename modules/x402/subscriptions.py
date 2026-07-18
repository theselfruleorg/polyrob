"""Watchtower subscriptions — prepaid periods + renewal invoices (Task 14,
Phase 3 R5, the first revenue wedge).

Model: PREPAID PERIOD + RENEWAL INVOICE (x402-native). A subscription row
records what a paying correspondent owes for one recurring cron job (the
"watchtower") to keep firing: an amount, a period, and how far the current
prepaid period extends (``paid_through``, an epoch second). The renewal +
lapse *mechanics* (creating the next renewal invoice, moving a lapsed sub
through grace -> suspended) live in the settlement watcher
(``modules/x402/settlement_watcher.py``) — this module is pure storage +
policy, driven entirely from that existing tick (no new ticker).

Storage: the SAME ``bot.db`` the other x402 tables live in (``subscriptions``
+ the idempotency ledger ``subscription_applied_settlements``, both created by
``modules.database.x402_tables.X402Tables.create_tables``), accessed via the
async ``DatabaseConnection`` wrapper — mirrors ``modules/x402/invoicing.py``'s
own access pattern exactly (NOT ``core/sqlite_util``'s raw-sqlite3 WAL helper,
which is for the SEPARATE goals.db/cron.db files; opening a second raw
``sqlite3.connect`` against the SAME bot.db file the async wrapper already
holds open would just fight it for locks).

Tenant-scoped reads/writes (``AND user_id = ?``); anonymous (empty) user_id is
refused at creation, mirroring ``invoicing.create_payment_request``. The
system-wide sweep queries (``subscriptions_needing_renewal`` /
``subscriptions_to_grace`` / ``subscriptions_to_suspend``) are deliberately
NOT tenant-scoped — they are the ticker's own cross-tenant pass, exactly like
``agents.task.goals.board.GoalBoard.ready()`` is not tenant-scoped.

Landmine (T3 pattern, repeated here on purpose): ``DatabaseConnection`` auto-
parses JSON-looking TEXT columns into dicts, so an exact match on
``metadata.subscription_id`` uses SQLite's ``json_extract`` — NEVER a
``metadata LIKE '%...%'`` substring match. A subscription id
(``sub_<12hex>``) contains no special LIKE wildcards itself, but LIKE's own
``_`` wildcard would still match any lookalike id that differs only in that
one character position (the same "u_abc matches uXabc" hazard documented in
``invoicing.py``).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_GRACE = "grace"
STATUS_SUSPENDED = "suspended"
STATUS_CANCELED = "canceled"
_STATUSES = (STATUS_ACTIVE, STATUS_GRACE, STATUS_SUSPENDED, STATUS_CANCELED)


class SettlementResult(str, enum.Enum):
    """Distinguishable :func:`apply_settlement` outcomes (Task 14 fix pass 2,
    Finding 1 — the lost-renewal-extension bug).

    - ``APPLIED`` — THIS call performed the ledger insert + ``paid_through``
      extension, atomically (both landed, or the caller would have seen an
      exception instead).
    - ``ALREADY_APPLIED`` — the ledger insert hit its PRIMARY KEY
      (``request_id``): a PRIOR call already fully applied this settlement.
      Because the ledger row and the extension now land in ONE transaction
      (see :func:`apply_settlement`), a PK conflict reliably means the
      extension happened too — never a partial state.
    - ``REFUSED`` — the F3 tenant-mismatch guard fired; no DB write was
      attempted at all.
    - ``UNKNOWN`` — ``subscription_id``/``request_id`` missing, or the
      subscription row does not exist; no DB write was attempted.

    ``APPLIED``/``ALREADY_APPLIED`` are the two SAFE outcomes for a caller
    deciding whether to proceed to claim the settlement wake — the extension
    is guaranteed to have landed. ``REFUSED``/``UNKNOWN`` are terminal but
    NOT safe: the underlying on-chain payment settled, yet the subscription
    could not be extended, so the caller must not treat these as a silent
    "settled, continue" wake. An EXCEPTION raised out of
    :func:`apply_settlement` (never a member of this enum) signals a
    RETRYABLE error — the caller should withhold the wake and retry the
    invoice on the next tick.
    """
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    REFUSED = "refused"
    UNKNOWN = "unknown"

# Statuses under which the subscription's cron job is permitted to run.
_WORK_PERMITTED_STATUSES = (STATUS_ACTIVE, STATUS_GRACE)


# --- flags -------------------------------------------------------------------

def subscriptions_enabled() -> bool:
    """SUBSCRIPTIONS_ENABLED — master gate for the whole watchtower-subscription
    mechanism: the settlement watcher's renewal/lapse processing AND the cron
    gate's lapsed-subscription skip. Default OFF. When off, nothing in this
    module is ever called by the watcher/cron runner — the subscriptions
    table stays untouched and behavior is byte-identical to today."""
    from core.env import bool_env
    return bool_env("SUBSCRIPTIONS_ENABLED", False)


def watchtower_price_usd() -> float:
    """WATCHTOWER_PRICE_USD — the default monthly price (USD) for a watchtower
    subscription; ``create_subscription`` falls back to this when no explicit
    ``amount_usd`` is given."""
    try:
        return float(os.getenv("WATCHTOWER_PRICE_USD", "10.00"))
    except ValueError:
        return 10.00


def subscription_renewal_lead_days() -> int:
    """SUBSCRIPTION_RENEWAL_LEAD_DAYS — how many days before ``paid_through``
    the renewal invoice is created (default 5)."""
    from core.env import int_env
    return max(0, int_env("SUBSCRIPTION_RENEWAL_LEAD_DAYS", 5))


def subscription_grace_days() -> int:
    """SUBSCRIPTION_GRACE_DAYS — how many days past ``paid_through`` a lapsed
    subscription keeps running (status ``grace``) before it is suspended
    (default 3)."""
    from core.env import int_env
    return max(0, int_env("SUBSCRIPTION_GRACE_DAYS", 3))


# --- plumbing ------------------------------------------------------------

async def _resolve_db(db=None):
    if db is not None:
        return db
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    return container.get_service("database_manager")


def _emit(kind: str, *, user_id: str, attrs: Optional[dict] = None) -> None:
    """First-class subscription telemetry (fail-open), same shape as
    ``invoicing._emit``."""
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                kind, user_id=user_id or "", session_id="",
                source="subscriptions", attrs=attrs or {},
            )
    except Exception:
        pass


def _row(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# --- CRUD ------------------------------------------------------------------

async def create_subscription(
    *,
    user_id: str,
    correspondent_surface: str,
    correspondent_address: str,
    cron_job_id: str,
    amount_usd: Optional[float] = None,
    period_days: int = 30,
    renewal_lead_days: Optional[int] = None,
    grace_days: Optional[int] = None,
    paid_through: Optional[int] = None,
    db=None,
) -> Dict[str, Any]:
    """Create an ``active`` subscription row — one prepaid period, starting
    now (``paid_through`` defaults to ``now + period_days`` — creation always
    represents a period already arranged/paid; pass an explicit
    ``paid_through`` to backdate, e.g. in tests)."""
    if not user_id:
        raise ValueError(
            "subscriptions require an authenticated tenant (empty user_id refused)")
    correspondent_surface = (correspondent_surface or "").strip()
    correspondent_address = (correspondent_address or "").strip()
    if not correspondent_surface or not correspondent_address:
        raise ValueError("correspondent_surface and correspondent_address are required")
    cron_job_id = (cron_job_id or "").strip()
    if not cron_job_id:
        raise ValueError("cron_job_id is required")

    database = await _resolve_db(db)
    if database is None:
        raise ValueError("subscriptions store unavailable (no database service)")

    sub_id = f"sub_{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    amount = float(amount_usd) if amount_usd is not None else watchtower_price_usd()
    if amount <= 0:
        raise ValueError("amount_usd must be positive")
    period = max(1, int(period_days))
    lead = subscription_renewal_lead_days() if renewal_lead_days is None else max(0, int(renewal_lead_days))
    grace = subscription_grace_days() if grace_days is None else max(0, int(grace_days))
    through = int(paid_through) if paid_through is not None else now + period * 86400

    await database.execute(
        """INSERT INTO subscriptions (
               id, user_id, correspondent_surface, correspondent_address, cron_job_id,
               amount_usd, period_days, paid_through, renewal_lead_days, grace_days,
               status, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (sub_id, user_id, correspondent_surface, correspondent_address, cron_job_id,
         amount, period, through, lead, grace, STATUS_ACTIVE),
    )
    _emit("subscription_created", user_id=user_id, attrs={
        "subscription_id": sub_id, "amount_usd": amount, "period_days": period,
        "cron_job_id": cron_job_id,
    })
    return await get_subscription(sub_id, db=database)


async def get_subscription(subscription_id: str, *, user_id: Optional[str] = None,
                           db=None) -> Optional[Dict[str, Any]]:
    """Fetch one subscription by id. When ``user_id`` is given, a row owned by
    a DIFFERENT tenant is treated as not found (tenant scoping)."""
    if not subscription_id:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,))
    sub = _row(row)
    if sub is None:
        return None
    if user_id is not None and sub.get("user_id") != user_id:
        return None
    return sub


async def list_subscriptions(*, user_id: str, status: Optional[str] = None,
                             db=None) -> List[Dict[str, Any]]:
    """Tenant-scoped listing, newest first. Empty/anonymous user_id -> []
    (mirrors invoicing.list_payment_requests's anonymous-bucket refusal)."""
    if not user_id:
        return []
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,))
    out = [dict(r) for r in rows or []]
    if status:
        out = [r for r in out if r.get("status") == status]
    return out


async def cancel_subscription(subscription_id: str, *, user_id: str, db=None) -> bool:
    """Tenant-scoped cancel: ``status -> canceled`` (terminal — the cron gate
    then $0-skips this subscription's job same as a suspended one). Returns
    False for a missing row, a wrong tenant, or an already-canceled row."""
    if not user_id or not subscription_id:
        return False
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        """UPDATE subscriptions SET status = ?, updated_at = datetime('now')
           WHERE id = ? AND user_id = ? AND status != ?""",
        (STATUS_CANCELED, subscription_id, user_id, STATUS_CANCELED),
    )
    ok = bool(getattr(cur, "rowcount", 0))
    if ok:
        _emit("subscription_canceled", user_id=user_id,
              attrs={"subscription_id": subscription_id})
    return ok


async def subscription_permits_work(subscription_id: str, *, db=None) -> bool:
    """The cron-gate predicate (``cron/runner.py``): True iff this
    subscription's status allows its cron job to run THIS tick.

    - active/grace -> True (grace still runs while a renewal is chased).
    - suspended/canceled -> False ($0 skip, ``subscription_lapsed``).
    - missing/dangling subscription_id -> True (permissive): a stale/typo'd
      payload reference must never silently block otherwise-legitimate work;
      only a RESOLVED non-active/grace status gates.
    """
    sub = await get_subscription(subscription_id, db=db)
    if sub is None:
        return True
    return sub.get("status") in _WORK_PERMITTED_STATUSES


# --- settlement application (idempotent) ------------------------------------

async def apply_settlement(subscription_id: str, request_id: str, *, db=None) -> SettlementResult:
    """A settled renewal invoice extends ``paid_through`` by the
    subscription's OWN ``period_days`` (read from its row, never trusted from
    the caller) and reactivates a grace/suspended row back to ``active``.

    Idempotent: keyed on ``request_id`` via the ``subscription_applied_settlements``
    PRIMARY KEY — a re-processed settlement (watcher retry, restart mid-tick)
    hits the UNIQUE constraint and is treated as "already applied", never as
    an error, and never double-extends.

    Task 14 fix pass 2 (Finding 1 — the lost-renewal-extension bug): the
    ledger INSERT and the ``paid_through`` UPDATE (+ the grace/suspended ->
    active reactivation, the SAME UPDATE) are wrapped in ONE transaction
    (``DatabaseConnection.begin_transaction``/``commit``/``rollback`` — the
    SAME pattern ``modules.credits.balance_manager`` uses; NOT the generic
    ``DatabaseConnection.transaction()`` convenience, which never sets
    ``_in_transaction`` and would let a concurrent, unrelated ``execute()``
    on the shared connection auto-commit mid-span). Previously these were two
    INDEPENDENTLY auto-committed statements: a crash / ``asyncio
    .CancelledError`` at shutdown / "database is locked" between them could
    commit the ledger row while the extension never applied. A retry then hit
    the ledger's PK, returned ``False`` WITHOUT raising — indistinguishable
    from a REFUSED tenant mismatch — and the (then-boolean) caller had no way
    to tell "already fully applied, safe to wake" apart from "never applied,
    do NOT wake"; either way it fell through to claim the settlement wake,
    and the subscription was stuck unrenewed forever with no error anywhere.
    With the atomic write, a failed second statement rolls back the ledger
    INSERT too, so a retry re-runs cleanly (no stale ledger row) and applies
    the extension exactly once.

    Returns a :class:`SettlementResult` — see its docstring for what each
    member means to a caller. Raises on a genuinely retryable DB error (the
    transaction is rolled back first; the caller should withhold the wake and
    retry next tick).
    """
    if not subscription_id or not request_id:
        return SettlementResult.UNKNOWN
    database = await _resolve_db(db)
    if database is None:
        return SettlementResult.UNKNOWN
    sub = await get_subscription(subscription_id, db=database)
    if sub is None:
        logger.warning("subscriptions: apply_settlement for unknown subscription "
                       "%s (request %s) — nothing to extend", subscription_id, request_id)
        return SettlementResult.UNKNOWN

    # Task 14 review Finding 3 (cheap defense-in-depth): confirm the settled
    # invoice's OWN tenant (read independently from its row via
    # invoicing.get_invoice_tenant — never trusted from the caller) matches
    # the subscription's user_id before extending. Unexploitable today (only
    # the settlement watcher ever writes metadata.subscription_id, always
    # matching the invoice's tenant), but a mismatch here would mean a
    # DIFFERENT tenant's payment silently extended someone else's
    # subscription — refuse loudly rather than trust blindly. A request_id
    # that doesn't resolve to a real invoice row (e.g. a synthetic id in a
    # unit test) is permissive — nothing to compare against, not a mismatch.
    # This check runs BEFORE any transaction is opened: a refusal must never
    # open/consume a transaction.
    from modules.x402 import invoicing
    invoice_tenant = await invoicing.get_invoice_tenant(request_id, db=database)
    if invoice_tenant is not None and invoice_tenant != sub.get("user_id"):
        logger.warning(
            "subscriptions: apply_settlement REFUSED — invoice %s tenant %r "
            "does not match subscription %s tenant %r (no extension applied, "
            "no wake claimed)", request_id, invoice_tenant, subscription_id,
            sub.get("user_id"))
        return SettlementResult.REFUSED

    await database.begin_transaction()
    try:
        cur = await database.execute(
            "INSERT INTO subscription_applied_settlements (request_id, subscription_id, applied_at) "
            "VALUES (?, ?, datetime('now'))",
            (request_id, subscription_id),
        )
    except sqlite3.IntegrityError:
        await database.rollback()
        # With the atomic write, a PK conflict now RELIABLY means a PRIOR
        # call already committed the ledger row AND the extension together
        # — never a partial state. Safe to treat as "already settled".
        logger.info("subscriptions: settlement %s already applied to %s — "
                    "skipping (idempotent)", request_id, subscription_id)
        return SettlementResult.ALREADY_APPLIED
    except (asyncio.CancelledError, Exception):
        # Task 14 fix pass 3 (re-review Finding — the transaction-state
        # leak): asyncio.CancelledError derives from BaseException (NOT
        # Exception) since Python 3.8, so a bare `except Exception` lets it
        # skip straight past the rollback if the settlement watcher ticker
        # is cancelled (autonomy-runtime shutdown force-cancel) while this
        # statement is in flight. An un-rolled-back transaction leaves
        # DatabaseConnection._in_transaction permanently True — poisoning
        # the SHARED bot.db connection for every other x402/credits/
        # user_profiles write forever (no more auto-commit) until process
        # restart. `except (asyncio.CancelledError, Exception)` (mirrors
        # core/tickers.py + core/autonomy_runtime.py's own idiom) still
        # rolls back and re-raises — cancellation is never swallowed, it
        # just no longer bypasses cleanup.
        await database.rollback()
        raise

    if not getattr(cur, "rowcount", 0):
        await database.rollback()
        return SettlementResult.UNKNOWN

    # M8 (lost extension under concurrent application): compute the new
    # paid_through IN SQL (read-modify-write in ONE atomic statement) rather
    # than from the pre-transaction Python read of `sub`. Two appliers for the
    # SAME subscription (different request_id, both ledger INSERTs succeed) used
    # to both read base=X and blind-write X+period — one paid period evaporated.
    # `COALESCE(paid_through, ?) + ?` appends the subscription's OWN period to
    # whatever the row currently holds, so applier1 does X->X+period and
    # applier2 does (X+period)->X+2period — both periods land. Preserves the
    # prepaid-append semantics (extend from the stored paid_through, `now` only
    # as the NULL fallback) — the review's normative M8 fix; NOT the brief's
    # `MAX(...,now)` paraphrase, which would reset a lapsed sub's base to `now`
    # and break the documented append model + the existing
    # test_settled_renewal_invoice_extends_subscription assertion.
    period_seconds = max(1, int(sub.get("period_days") or 30)) * 86400
    now_epoch = int(time.time())
    try:
        await database.execute(
            "UPDATE subscriptions SET paid_through = COALESCE(paid_through, ?) + ?, "
            "status = ?, updated_at = datetime('now') WHERE id = ?",
            (now_epoch, period_seconds, STATUS_ACTIVE, subscription_id),
        )
    except (asyncio.CancelledError, Exception):
        # Retryable: rolling back here undoes the ledger INSERT too, so the
        # NEXT call re-runs cleanly with no stale ledger row — this is the
        # crux of the Finding 1 fix. Also catches asyncio.CancelledError
        # explicitly (fix pass 3 — see the twin comment above): otherwise a
        # cancellation mid-UPDATE would leak _in_transaction=True forever.
        await database.rollback()
        raise

    await database.commit()
    # Re-read the atomically-computed paid_through for the telemetry emit (the
    # UPDATE computed it in SQL, so it isn't available in Python). Best-effort —
    # telemetry, never money-critical.
    emitted_through: Optional[int] = None
    try:
        refreshed = await get_subscription(subscription_id, db=database)
        if refreshed is not None and refreshed.get("paid_through") is not None:
            emitted_through = int(refreshed["paid_through"])
    except Exception:
        emitted_through = None
    _emit("subscription_renewed", user_id=sub.get("user_id") or "", attrs={
        "subscription_id": subscription_id, "request_id": request_id,
        "paid_through": emitted_through,
    })
    return SettlementResult.APPLIED


# --- renewal / lapse queries (system-wide sweep, no tenant filter) ---------

async def _has_open_renewal_invoice(subscription_id: str, *, db=None) -> bool:
    database = await _resolve_db(db)
    if database is None:
        return False
    row = await database.fetch_one(
        """SELECT COUNT(*) AS n FROM x402_payment_requests
           WHERE status = 'pending'
             AND json_extract(metadata, '$.subscription_id') = ?""",
        (subscription_id,),
    )
    if not row:
        return False
    try:
        return bool(int(row.get("n") or 0))
    except (AttributeError, TypeError):
        return bool(row[0])


async def subscriptions_needing_renewal(*, now: Optional[float] = None,
                                        db=None) -> List[Dict[str, Any]]:
    """active/grace subscriptions inside their renewal lead window
    (``paid_through - renewal_lead_days*86400 < now``) with NO already-open
    (pending) renewal invoice outstanding. System-wide sweep — no tenant
    filter, mirrors ``GoalBoard.ready()``."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = now if now is not None else time.time()
    rows = await database.fetch_all(
        """SELECT * FROM subscriptions
           WHERE status IN (?, ?)
             AND (paid_through - renewal_lead_days * 86400) < ?""",
        (STATUS_ACTIVE, STATUS_GRACE, cutoff),
    )
    out = []
    for row in rows or []:
        sub = dict(row)
        if await _has_open_renewal_invoice(sub["id"], db=database):
            continue
        out.append(sub)
    return out


async def subscriptions_to_grace(*, now: Optional[float] = None, db=None) -> List[Dict[str, Any]]:
    """Flip every ``active`` subscription past its ``paid_through`` to
    ``grace`` (CAS-guarded so a row is only ever flipped once) and emit
    ``subscription_grace`` for each. Returns the flipped rows (post-flip)."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = now if now is not None else time.time()
    rows = await database.fetch_all(
        "SELECT * FROM subscriptions WHERE status = ? AND paid_through < ?",
        (STATUS_ACTIVE, cutoff),
    )
    out = []
    for row in rows or []:
        sub = dict(row)
        cur = await database.execute(
            "UPDATE subscriptions SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (STATUS_GRACE, sub["id"], STATUS_ACTIVE),
        )
        if not getattr(cur, "rowcount", 0):
            continue  # lost a race (already transitioned) — not our flip to report
        sub["status"] = STATUS_GRACE
        _emit("subscription_grace", user_id=sub.get("user_id") or "",
              attrs={"subscription_id": sub["id"], "paid_through": sub.get("paid_through")})
        out.append(sub)
    return out


async def subscriptions_to_suspend(*, now: Optional[float] = None, db=None) -> List[Dict[str, Any]]:
    """Flip every active/grace subscription past its grace deadline
    (``paid_through + grace_days*86400 < now``) to ``suspended``. Pure state
    transition ONLY — no event emission, no notices: the caller
    (``SettlementWatcher._notify_suspended``) owns the ONE
    ``subscription_suspended`` event + the owner/correspondent notices, so
    the event isn't double-emitted between here and there."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = now if now is not None else time.time()
    rows = await database.fetch_all(
        """SELECT * FROM subscriptions WHERE status IN (?, ?)
           AND (paid_through + grace_days * 86400) < ?""",
        (STATUS_ACTIVE, STATUS_GRACE, cutoff),
    )
    out = []
    for row in rows or []:
        sub = dict(row)
        cur = await database.execute(
            "UPDATE subscriptions SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status IN (?, ?)",
            (STATUS_SUSPENDED, sub["id"], STATUS_ACTIVE, STATUS_GRACE),
        )
        if not getattr(cur, "rowcount", 0):
            continue
        sub["status"] = STATUS_SUSPENDED
        out.append(sub)
    return out
