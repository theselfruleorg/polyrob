"""Agent-initiated x402 payment requests — the invoice side of the money loop.

Until now only the HTTP middleware could produce `x402_payment_requests` rows,
and only for already-settled platform charges. This module lets the AGENT create
a *pending* payment request (an invoice) from inside a session: amount, purpose,
optional payer hint, expiry — riding the existing table, treasury config
(`X402_PAYMENT_RECIPIENT` / `X402_DEFAULT_CHAIN`) and telemetry event log.

Invoice rows are distinguished by ``metadata.kind == "agent_invoice"`` and carry
the originating ``session_id`` so the settlement watcher
(`modules/x402/settlement_watcher.py`) can re-enter that session when the
invoice settles. Settlement itself is an explicit, attested transition
(``settle_payment_request`` — owner CLI / API / a future payable endpoint), never
inferred; expiry is enforced by the watcher via the row's ``deadline``.

Rails: amounts are bounded by ``X402_INVOICE_MAX_USD`` (an
absurd invoice is a reputation incident) and creation is capped per tenant per
day (``X402_INVOICE_DAILY_MAX``). Deliberately NOT recorded into the wallet
PolicyGate spend audit — receivables in the spend window would corrupt the 24h
rolling spend caps. Every creation/settlement/expiry emits a first-class
telemetry event (``payment_requested`` / ``payment_settled`` / ``payment_expired``).
"""
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INVOICE_KIND = "agent_invoice"


def invoice_max_usd() -> float:
    try:
        return float(os.getenv("X402_INVOICE_MAX_USD", "50"))
    except ValueError:
        return 50.0


def invoice_daily_max() -> int:
    try:
        return int(os.getenv("X402_INVOICE_DAILY_MAX", "10"))
    except ValueError:
        return 10


def x402_invoicing_enabled() -> bool:
    from core.env import bool_env
    return bool_env("X402_INVOICE_ENABLED", False)


async def _resolve_db(db=None):
    if db is not None:
        return db
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    return container.get_service("database_manager")


def _emit(kind: str, *, user_id: str, session_id: str = "", attrs: Optional[dict] = None) -> None:
    """First-class money telemetry (fail-open). attrs passed as an explicit dict —
    the record() reserved-kwarg collision landmine."""
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if event_log_enabled():
            get_event_log().record(
                kind, user_id=user_id, session_id=session_id,
                source="x402_invoice", attrs=attrs or {},
            )
    except Exception:
        pass


def _row_metadata(row: Dict[str, Any]) -> dict:
    """metadata column, tolerant of both shapes: DatabaseConnection.fetch_* auto-
    parses JSON-looking TEXT into a dict; raw sqlite rows give the str."""
    meta = row.get("metadata")
    if isinstance(meta, dict):
        return meta
    try:
        return json.loads(meta or "{}")
    except Exception:
        return {}


def is_invoice_row(row: Dict[str, Any]) -> bool:
    return _row_metadata(row).get("kind") == INVOICE_KIND


def _sanitize_correspondent_ref(ref: Optional[Dict[str, Any]]) -> Optional[dict]:
    """Keep only the correspondent registry key fields (surface/address/thread_id),
    stringified and bounded, so a settled invoice can be delivered as DATA on the
    correspondent rail rather than as an owner self-wake. None when absent/invalid."""
    if not isinstance(ref, dict):
        return None
    surface = str(ref.get("surface") or "").strip()[:64]
    address = str(ref.get("address") or "").strip()[:256]
    if not surface or not address:
        return None
    return {"surface": surface, "address": address,
            "thread_id": str(ref.get("thread_id") or "").strip()[:128]}


async def create_payment_request(
    *,
    user_id: str,
    session_id: str,
    amount_usd: float,
    purpose: str,
    payer_hint: Optional[str] = None,
    expiry_hours: float = 72.0,
    correspondent_ref: Optional[Dict[str, Any]] = None,
    db=None,
) -> Dict[str, Any]:
    """Create a pending invoice row. Returns payment instructions, or raises
    ValueError with an agent-readable reason (caps, config, validation)."""
    if not user_id:
        # An empty tenant would create a SHARED anonymous invoice bucket (cross-
        # tenant reads + a shared daily cap) — refuse, mirroring MEMORY_REQUIRE_USER_ID.
        raise ValueError("invoicing requires an authenticated tenant (empty user_id refused)")
    if not purpose or not purpose.strip():
        raise ValueError("purpose is required — the payer must know what they are paying for")
    amount_usd = float(amount_usd)
    if amount_usd <= 0:
        raise ValueError("amount_usd must be positive")
    cap = invoice_max_usd()
    if amount_usd > cap:
        raise ValueError(
            f"amount ${amount_usd:.2f} exceeds the invoice ceiling ${cap:.2f} "
            "(X402_INVOICE_MAX_USD)"
        )
    from modules.x402.x402_integration import get_x402_config
    cfg = get_x402_config()
    recipient = (cfg.get("pay_to") or "").strip()
    if not recipient:
        raise ValueError("no treasury configured — set X402_PAYMENT_RECIPIENT")

    database = await _resolve_db(db)
    if database is None:
        raise ValueError("payment-request store unavailable (no database service)")

    daily_cap = invoice_daily_max()
    # Tenant match covers both storage shapes: user_id column when the tenant has
    # a user_profiles row, metadata.tenant_id when the FK fallback stored NULL.
    row = await database.fetch_one(
        """SELECT COUNT(*) AS n FROM x402_payment_requests
           WHERE (user_id = ? OR metadata LIKE ?)
             AND created_at >= datetime('now', '-1 day')
             AND metadata LIKE ?""",
        (user_id, f'%"tenant_id": "{user_id}"%', f'%"kind": "{INVOICE_KIND}"%'),
    )
    if row and int(row.get("n") or 0) >= daily_cap:
        raise ValueError(
            f"daily invoicing cap reached ({daily_cap}/day, X402_INVOICE_DAILY_MAX)"
        )

    request_id = f"inv_{uuid.uuid4().hex[:12]}"
    nonce = f"inv_{uuid.uuid4().hex}"
    expiry_hours = max(0.1, float(expiry_hours))
    deadline = int(time.time() + expiry_hours * 3600)
    metadata = json.dumps({
        "kind": INVOICE_KIND,
        "session_id": session_id,
        "tenant_id": user_id,
        "purpose": purpose.strip()[:500],
        "payer_hint": (payer_hint or "").strip()[:200] or None,
        "wake_delivered": False,
        "correspondent_ref": _sanitize_correspondent_ref(correspondent_ref),
    })
    chain = cfg.get("network") or "base"
    # user_id has an FK to user_profiles; an agent tenant (e.g. "rob") may not
    # exist there. Pre-check and store NULL in the column when absent — the
    # tenant stays queryable via metadata.tenant_id (every reader matches both).
    column_user: Optional[str] = user_id
    try:
        profile = await database.fetch_one(
            "SELECT 1 AS ok FROM user_profiles WHERE user_id = ?", (user_id,))
        if not profile:
            column_user = None
    except Exception:
        column_user = None
    await database.execute(
        """INSERT INTO x402_payment_requests (
               id, user_id, amount, amount_usd, asset, chain, recipient, nonce,
               deadline, status, metadata, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (request_id, column_user, str(amount_usd), amount_usd, "usdc", chain,
         recipient.lower(), nonce, deadline, "pending", metadata),
    )

    _emit("payment_requested", user_id=user_id, session_id=session_id, attrs={
        "request_id": request_id, "amount_usd": amount_usd, "chain": chain,
        "purpose": purpose.strip()[:200], "deadline": deadline,
    })
    return {
        "request_id": request_id,
        "amount_usd": amount_usd,
        "asset": "usdc",
        "chain": chain,
        "recipient": recipient,
        "purpose": purpose.strip(),
        "expires_at_epoch": deadline,
        "status": "pending",
    }


async def list_payment_requests(
    *, user_id: str, status: Optional[str] = None, limit: int = 20, db=None,
) -> List[Dict[str, Any]]:
    """Tenant-scoped invoice listing (newest first). Matches the tenant either by
    the user_id column or metadata.tenant_id (the FK-fallback path). An empty
    user_id returns nothing (the anonymous bucket is refused at creation too)."""
    if not user_id:
        return []
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE (user_id = ? OR metadata LIKE ?) AND metadata LIKE ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, f'%"tenant_id": "{user_id}"%', f'%"kind": "{INVOICE_KIND}"%',
         max(1, int(limit))),
    )
    out = []
    for row in rows or []:
        if status and row.get("status") != status:
            continue
        meta = _row_metadata(row)
        out.append({
            "request_id": row.get("id"),
            "amount_usd": row.get("amount_usd"),
            "status": row.get("status"),
            "purpose": meta.get("purpose"),
            "session_id": meta.get("session_id"),
            "chain": row.get("chain"),
            "created_at": row.get("created_at"),
            "completed_at": row.get("completed_at"),
            "deadline": row.get("deadline"),
        })
    return out


async def get_payment_request(request_id: str, *, db=None) -> Optional[Dict[str, Any]]:
    """Read one invoice row by id — the payable endpoint needs the amount, chain,
    recipient and status to build a per-invoice PaymentRequirements challenge.

    Public (no tenant scoping): a third-party payer legitimately does not know the
    invoice's owning tenant. Returns None for a missing row or a non-invoice row."""
    if not request_id:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (request_id,))
    if not row or not is_invoice_row(row):
        return None
    meta = _row_metadata(row)
    return {
        "request_id": row.get("id"),
        "amount_usd": row.get("amount_usd"),
        "asset": row.get("asset"),
        "chain": row.get("chain"),
        "recipient": row.get("recipient"),
        "nonce": row.get("nonce"),
        "deadline": row.get("deadline"),
        "status": row.get("status"),
        "purpose": meta.get("purpose") or "",
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


async def claim_for_settlement(request_id: str, *, db=None) -> bool:
    """Atomic pending→settling CAS — the exclusive right to settle ONE invoice.

    The payable endpoint claims BEFORE calling the facilitator so two concurrent
    distinct payers can never both settle the same invoice on-chain: the loser's
    claim fails (rowcount 0) and it never touches the facilitator. On a facilitator
    failure the winner reverts via :func:`revert_settlement_claim`."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        "UPDATE x402_payment_requests SET status = 'settling', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'pending'",
        (request_id,),
    )
    return bool(getattr(cur, "rowcount", 0))


async def revert_settlement_claim(request_id: str, *, db=None) -> None:
    """Revert settling→pending after a facilitator/verify failure (payable again).

    Only a row THIS caller left in 'settling' is reverted (guarded on status), so a
    concurrently-completed row is never resurrected."""
    database = await _resolve_db(db)
    if database is None:
        return
    await database.execute(
        "UPDATE x402_payment_requests SET status = 'pending', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'settling'",
        (request_id,),
    )


async def settle_payment_request(
    request_id: str, *, transaction_hash: Optional[str] = None, db=None,
) -> bool:
    """Attested transition to completed (owner CLI / API). Idempotent: only a
    'pending' (owner-attested direct settle) or 'settling' (endpoint post-claim) row
    transitions; anything else returns False. The settlement WAKE + payment_settled
    event are the watcher's job (it runs in the agent process; this may not)."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        """UPDATE x402_payment_requests
           SET status = 'completed', transaction_hash = ?,
               completed_at = datetime('now'), updated_at = datetime('now')
           WHERE id = ? AND status IN ('pending', 'settling')""",
        (transaction_hash, request_id),
    )
    settled = bool(getattr(cur, "rowcount", 0))
    if settled:
        logger.info("x402 invoice %s settled (tx=%s)", request_id, transaction_hash)
    return settled


async def expire_stale_requests(*, db=None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """pending + past deadline → expired. Returns the expired invoice dicts and
    emits one payment_expired event each."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = int(now if now is not None else time.time())
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'pending' AND deadline < ? AND metadata LIKE ?""",
        (cutoff, f'%"kind": "{INVOICE_KIND}"%'),
    )
    expired = []
    for row in rows or []:
        cur = await database.execute(
            "UPDATE x402_payment_requests SET status='expired', updated_at=datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        if not getattr(cur, "rowcount", 0):
            # lost the race to a concurrent settle — not actually expired; never
            # emit a false payment_expired for a payment that was in fact paid
            continue
        meta = _row_metadata(row)
        tenant = meta.get("tenant_id") or row.get("user_id") or ""
        _emit("payment_expired", user_id=tenant,
              session_id=meta.get("session_id") or "", attrs={
                  "request_id": row["id"], "amount_usd": row.get("amount_usd")})
        expired.append({"request_id": row["id"], "amount_usd": row.get("amount_usd"),
                        "session_id": meta.get("session_id"), "user_id": tenant,
                        "purpose": meta.get("purpose")})
    return expired


async def settled_unnotified_invoices(*, db=None) -> List[Dict[str, Any]]:
    """Settled agent invoices whose originating session has not been woken yet."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status IN ('completed', 'settled_no_tx')
             AND metadata LIKE ? AND metadata LIKE ?""",
        (f'%"kind": "{INVOICE_KIND}"%', '%"wake_delivered": false%'),
    )
    out = []
    for row in rows or []:
        meta = _row_metadata(row)
        out.append({
            "request_id": row["id"],
            "amount_usd": row.get("amount_usd"),
            "transaction_hash": row.get("transaction_hash"),
            "session_id": meta.get("session_id") or "",
            "user_id": meta.get("tenant_id") or row.get("user_id") or "",
            "purpose": meta.get("purpose") or "",
            "correspondent_ref": meta.get("correspondent_ref") or None,
        })
    return out


async def claim_wake(request_id: str, *, db=None) -> bool:
    """Atomically claim the settlement notification for one invoice.

    A string REPLACE on the machine-written ``"wake_delivered": false`` token,
    guarded by a LIKE on the same token and checked via rowcount — so when two
    watcher processes race, exactly ONE wins the claim and delivers the wake/
    event (claim-then-notify, never notify-then-mark)."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        """UPDATE x402_payment_requests
           SET metadata = REPLACE(metadata, '"wake_delivered": false',
                                            '"wake_delivered": true'),
               updated_at = datetime('now')
           WHERE id = ? AND metadata LIKE '%"wake_delivered": false%'""",
        (request_id,),
    )
    return bool(getattr(cur, "rowcount", 0))


async def mark_wake_delivered(request_id: str, *, db=None) -> None:
    """Back-compat wrapper over :func:`claim_wake` (ignores the claim result)."""
    await claim_wake(request_id, db=db)
