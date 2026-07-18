"""Agent-initiated x402 payment requests — the invoice side of the money loop.

Until now only the HTTP middleware could produce `x402_payment_requests` rows,
and only for already-settled platform charges. This module lets the AGENT create
a *pending* payment request (an invoice) from inside a session: amount, purpose,
an optional free-form ``payer_contact`` (the payer's own contact info, shown on
the invoice — no contact book, no schema change), expiry — riding the existing
table, treasury config (`X402_PAYMENT_RECIPIENT` / `X402_DEFAULT_CHAIN`) and
telemetry event log.

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
import asyncio
import json
import logging
import os
import sqlite3
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
    """Gate for the whole agent-invoicing surface: the `x402_invoice` tool
    (tools/x402/__init__.py delegates here — this is the shared SSOT so the two
    never disagree), the public settlement/pay endpoints (api/x402_endpoints.py),
    and the autonomy-runtime settlement watcher startup gate. Default OFF; ON
    under effective AUTONOMY_MODE=autonomous via _mode_capability_default (013 T2
    review fix, Finding 2 — was raw-env-only, leaving invoices creatable but
    unpayable/unsettleable under autonomous mode). Explicit X402_INVOICE_ENABLED
    always wins. Lazy + guarded import, fail-closed to OFF on any fault.

    core/autonomy_runtime.py does NOT import this (a core-tier module importing
    modules.x402 would put a server-tier module on the core import graph — the
    C3 boundary, see tests/test_core_server_boundary.py) and instead applies the
    same guarded-OR locally."""
    from core.env import bool_env
    try:
        from core.config_policy import _mode_capability_default
        default = _mode_capability_default("X402_INVOICE_ENABLED")
    except Exception:
        default = False
    return bool_env("X402_INVOICE_ENABLED", default)


def x402_settle_onchain_detect_enabled() -> bool:
    """Task 11 (Phase 2): whether the settlement watcher additionally scans
    the treasury address for plain USDC transfers (no facilitator) and
    auto-settles the matching pending invoice. Default OFF — the watcher
    ALSO requires a configured mainnet chain + treasury before it actually
    scans (see `settlement_watcher.py::SettlementWatcher._scan_onchain`);
    this getter is the single flag-parse SSOT shared by that gate and the
    amount-jitter gate below."""
    from core.env import bool_env
    return bool_env("X402_SETTLE_ONCHAIN_DETECT", False)


def x402_invoice_amount_jitter_enabled() -> bool:
    """Whether `create_payment_request` nudges a colliding amount (Task 11).
    Default ON, but INERT unless on-chain detection is also enabled
    (`x402_settle_onchain_detect_enabled`) — the jitter exists solely to keep
    on-chain amount-matching unambiguous; with detection off it would just be
    a pointless amount perturbation. See `_dedupe_amount_for_treasury`.

    I2 safety fix (Task 11 review): this flag can no longer DISABLE jitter
    while detection is ON — ``X402_SETTLE_ONCHAIN_DETECT=true`` +
    ``X402_INVOICE_AMOUNT_JITTER=false`` is an unsafe combination (auto-
    settlement from on-chain transfers with ZERO disambiguation), so
    `create_payment_request` forces jitter on internally in that case and logs
    a one-time-per-call notice. Setting this false only has effect (staying
    truly inert) while detection is also off — see `_jitter_should_apply`."""
    from core.env import bool_env
    return bool_env("X402_INVOICE_AMOUNT_JITTER", True)


def _jitter_should_apply() -> bool:
    """The ACTUAL jitter gate `create_payment_request` uses (I2 fix): jitter
    is forced ON whenever on-chain detection is on, regardless of the
    ``X402_INVOICE_AMOUNT_JITTER`` value — logging a notice when the flag was
    explicitly set to disable it. When detection is off, jitter stays fully
    inert (byte-identical legacy amounts) exactly as before."""
    detect_on = x402_settle_onchain_detect_enabled()
    if not detect_on:
        return False
    if not x402_invoice_amount_jitter_enabled():
        logger.warning(
            "X402_SETTLE_ONCHAIN_DETECT is on but X402_INVOICE_AMOUNT_JITTER "
            "is explicitly off — forcing amount-collision jitter ON anyway: "
            "on-chain auto-settlement with no jitter cannot disambiguate a "
            "same-amount pending-invoice collision (Task 11 I2 safety fix)."
        )
    return True


# Per-treasury in-process locks (I1 fix): serialize the amount-collision
# dedupe SELECT + INSERT critical section in `create_payment_request` so two
# concurrent creates for the SAME treasury+amount can never both observe "no
# collision" before either has inserted — closing the TOCTOU window that
# would otherwise let both keep the exact, unjittered amount (defeating the
# whole point of the jitter). Scoped to a single process (POLYROB's default
# `UVICORN_WORKERS=1` deployment model — see the session-registry SQLite
# backend for the cross-process class of this problem, which is out of scope
# here); a module-level dict is fine since treasuries are few and long-lived.
_treasury_locks: Dict[str, asyncio.Lock] = {}


def _treasury_lock(recipient: str) -> asyncio.Lock:
    lock = _treasury_locks.get(recipient)
    if lock is None:
        lock = asyncio.Lock()
        _treasury_locks[recipient] = lock
    return lock


# M5: the partial UNIQUE index name — the CROSS-process backstop the in-process
# `_treasury_lock` cannot provide under UVICORN_WORKERS>1 (each worker runs its
# own settlement watcher and can create a same-(recipient, amount) invoice
# concurrently; the in-process lock only serializes within one process).
_PENDING_AMOUNT_INDEX = "idx_x402_requests_pending_amount_unique"


def _is_pending_amount_conflict(err: Exception) -> bool:
    """True when an ``IntegrityError`` is the M5 pending-amount unique
    violation. SQLite reports a UNIQUE index on plain COLUMNS by the column
    names — NOT the index name (that form is reserved for indexes on
    expressions, like the subscription index ``json_extract(...)``), so match
    on the ``(recipient, amount_usd)`` column signature."""
    msg = str(err)
    return ("UNIQUE constraint failed" in msg
            and "recipient" in msg and "amount_usd" in msg)


async def _ensure_pending_amount_unique_index(database) -> None:
    """Create the M5 partial UNIQUE index on ``(recipient, amount_usd)`` for
    PENDING agent invoices. Created ONLY on the jitter-active path (on-chain
    detection ON) — with detection OFF, two same-amount pending invoices are
    INTENTIONALLY allowed (byte-identical legacy; see the note in
    ``modules.database.x402_tables.X402Tables.create_tables``), so the index
    must NOT exist to enforce uniqueness in that case.

    Mirrors the SHAPE of ``x402_tables.dedupe_and_create_tx_hash_unique_index``
    (self-healing ``CREATE ... IF NOT EXISTS``, degrade-not-crash). It does NOT
    mutate an existing pending invoice's amount to clear a legacy duplicate (a
    payer may already have been quoted the old amount — changing it silently
    could misdirect their payment). If the index cannot be created because such
    duplicates already exist, it degrades to the in-process ``_treasury_lock``
    guard with a loud log rather than raising into the create path — strictly
    no worse than today (today has no index at all)."""
    try:
        await database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_PENDING_AMOUNT_INDEX} "
            "ON x402_payment_requests(recipient, amount_usd) "
            "WHERE status = 'pending' "
            "AND json_extract(metadata, '$.kind') = 'agent_invoice'"
        )
    except Exception as e:
        logger.warning(
            "x402 invoicing: could not create %s (likely pre-existing legacy "
            "duplicate same-amount pending agent invoices) — degrading to the "
            "in-process per-treasury lock; workers>1 same-amount collision "
            "protection is reduced until those duplicates clear: %s",
            _PENDING_AMOUNT_INDEX, e)


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


async def _dedupe_amount_for_treasury(
    amount_usd: float, recipient: str, cap: float, database,
) -> float:
    """Task 11 amount-collision jitter: if a PENDING agent invoice for this
    treasury already carries the exact same amount, nudge by deterministic
    whole steps of $0.0001 (sub-cent) until unique among pending invoices for
    this treasury, or the cap is reached. Returns the ORIGINAL amount
    unchanged when there is no collision (the common case, zero-cost) or when
    every candidate up to the cap is still colliding/over — in which case the
    watcher's oldest-first ambiguity policy is the fallback disambiguator."""
    step = 0.0001
    for i in range(100):
        candidate = round(amount_usd + step * i, 6)
        if candidate > cap:
            break
        row = await database.fetch_one(
            """SELECT COUNT(*) AS n FROM x402_payment_requests
               WHERE status = 'pending' AND recipient = ? AND amount_usd = ?
                 AND json_extract(metadata, '$.kind') = ?""",
            (recipient, candidate, INVOICE_KIND),
        )
        if not row or not int(row.get("n") or 0):
            return candidate
    return amount_usd


async def create_payment_request(
    *,
    user_id: str,
    session_id: str,
    amount_usd: float,
    purpose: str,
    payer_contact: Optional[str] = None,
    payer_hint: Optional[str] = None,
    expiry_hours: float = 72.0,
    correspondent_ref: Optional[Dict[str, Any]] = None,
    subscription_id: Optional[str] = None,
    db=None,
) -> Dict[str, Any]:
    """Create a pending invoice row. Returns payment instructions, or raises
    ValueError with an agent-readable reason (caps, config, validation).

    ``payer_contact`` is a free-form "billed to" string (name/email/handle),
    stored and surfaced verbatim — no contact book, no schema change.
    ``payer_hint`` is a deprecated alias kept for back-compat; when both are
    given ``payer_contact`` wins.

    ``subscription_id`` (Task 14): when this invoice is a watchtower
    subscription's renewal, the id rides in ``metadata.subscription_id`` so
    the settlement watcher can detect it on settlement and call
    ``modules.x402.subscriptions.apply_settlement`` — extending the
    subscription's ``paid_through`` instead of treating it as an ordinary
    one-off invoice. None for every other invoice (unchanged legacy shape)."""
    # H5: the owner kill-switch halts ALL autonomous activity — including minting new
    # payment requests (agent invoices AND the settlement watcher's auto-mode renewals),
    # not just outbound spend. Fail closed: a probe error blocks creation.
    try:
        from core.config_policy import AutonomyConfig
        halted = AutonomyConfig.autonomy_halted()
    except Exception as e:
        raise ValueError(f"invoicing refused: kill-switch probe failed ({e}) — failing closed")
    if halted:
        raise ValueError("invoicing refused: autonomy is HALTED (owner kill-switch)")
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
    # json_extract (SQLite JSON1, bundled) not LIKE: a LIKE pattern treats `_`/`%`
    # as wildcards, and real tenant ids contain underscores (u_<hex>), so
    # 'u_abc' would also match a lookalike 'uXabc' row (G-14). The kind filter is
    # ALSO json_extract (L8): the boot-time subscription dedup rewrites metadata
    # via json_set -> compact JSON ("kind":"agent_invoice", no space), which the
    # old spaced LIKE '%"kind": "agent_invoice"%' silently stops matching.
    row = await database.fetch_one(
        """SELECT COUNT(*) AS n FROM x402_payment_requests
           WHERE (user_id = ? OR json_extract(metadata, '$.tenant_id') = ?)
             AND created_at >= datetime('now', '-1 day')
             AND json_extract(metadata, '$.kind') = ?""",
        (user_id, user_id, INVOICE_KIND),
    )
    if row and int(row.get("n") or 0) >= daily_cap:
        raise ValueError(
            f"daily invoicing cap reached ({daily_cap}/day, X402_INVOICE_DAILY_MAX)"
        )

    request_id = f"inv_{uuid.uuid4().hex[:12]}"
    nonce = f"inv_{uuid.uuid4().hex}"
    expiry_hours = max(0.1, float(expiry_hours))
    deadline = int(time.time() + expiry_hours * 3600)
    contact = (payer_contact or payer_hint or "").strip()[:200] or None
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

    async def _insert(final_amount: float) -> None:
        metadata = json.dumps({
            "kind": INVOICE_KIND,
            "session_id": session_id,
            "tenant_id": user_id,
            "purpose": purpose.strip()[:500],
            "payer_contact": contact,
            "wake_delivered": False,
            "correspondent_ref": _sanitize_correspondent_ref(correspondent_ref),
            "subscription_id": subscription_id,
        })
        try:
            await database.execute(
                """INSERT INTO x402_payment_requests (
                       id, user_id, amount, amount_usd, asset, chain, recipient, nonce,
                       deadline, status, metadata, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                (request_id, column_user, str(final_amount), final_amount, "usdc", chain,
                 recipient.lower(), nonce, deadline, "pending", metadata),
            )
        except sqlite3.IntegrityError as e:
            # Task 14 review Finding 2 (duplicate-renewal TOCTOU): with
            # `subscription_id` set, the ONLY unique constraint this INSERT can
            # plausibly hit (besides an astronomically-unlikely random-uuid
            # nonce collision) is
            # `idx_x402_requests_pending_subscription_unique` — the partial
            # index enforcing at most one PENDING renewal invoice per
            # subscription (self-healed by
            # `modules.database.x402_tables.dedupe_and_create_subscription_pending_unique_index`).
            # Two concurrent watcher instances (`UVICORN_WORKERS>1`) can both
            # pass `subscriptions._has_open_renewal_invoice`'s plain SELECT
            # before either has inserted; the index is the atomic backstop —
            # the loser must be treated as "a pending renewal already exists",
            # never crash/propagate a raw IntegrityError to the caller.
            if subscription_id and "idx_x402_requests_pending_subscription_unique" in str(e):
                logger.info(
                    "x402 renewal-invoice create for subscription %s hit the "
                    "pending-renewal unique index (a pending renewal invoice "
                    "already exists) — refusing to create a duplicate, "
                    "concurrent-create TOCTOU closed by the index", subscription_id)
                raise ValueError(
                    f"a pending renewal invoice already exists for subscription "
                    f"{subscription_id} — refusing to create a second"
                ) from e
            raise

    # Task 11: keep on-chain amount-matching unambiguous. Inert (amount_usd
    # untouched) unless on-chain detection is on (`_jitter_should_apply` —
    # the I2 fix forces jitter on whenever detection is on, regardless of the
    # jitter flag). I1 fix: the collision-check (SELECT) and the INSERT are
    # two separate awaited DB calls — without a lock, two concurrent creates
    # for the SAME treasury+amount can both observe "no collision" before
    # either has inserted, so BOTH keep the exact amount (defeating the
    # jitter). Hold the per-treasury lock across the dedupe SELECT + INSERT
    # so the second racer's SELECT always sees the first racer's row.
    if _jitter_should_apply():
        # M5: the partial UNIQUE index is the CROSS-process backstop for the
        # in-process dedupe below. Created only here (jitter-active path).
        await _ensure_pending_amount_unique_index(database)
        async with _treasury_lock(recipient.lower()):
            candidate = await _dedupe_amount_for_treasury(
                amount_usd, recipient.lower(), cap, database)
            # The dedupe SELECT closes the SAME-process TOCTOU; the index closes
            # the CROSS-process one (workers>1). If a concurrent worker inserted
            # this exact (recipient, amount) between our SELECT and INSERT, the
            # INSERT raises IntegrityError on the pending-amount index — bump to
            # the next jitter candidate and retry, exactly as the in-process
            # dedupe would have (M5 IntegrityError-retry).
            attempts = 0
            while True:
                try:
                    await _insert(candidate)
                    break
                except sqlite3.IntegrityError as e:
                    if not _is_pending_amount_conflict(e):
                        raise
                    attempts += 1
                    if attempts > 100:
                        raise ValueError(
                            "could not find a unique pending-invoice amount for "
                            "this treasury after 100 jitter steps (too many "
                            "same-amount pending invoices)")
                    candidate = round(candidate + 0.0001, 6)
                    if candidate > cap:
                        raise ValueError(
                            f"amount ${candidate:.4f} exceeds the invoice ceiling "
                            f"${cap:.2f} while jittering past a same-amount "
                            "collision (X402_INVOICE_MAX_USD)")
            amount_usd = candidate
    else:
        # The M5 partial-unique index (`_PENDING_AMOUNT_INDEX`) is created
        # ONLY on the jitter-active path above — but once created it is never
        # dropped when jitter/detection is later disabled (self-healing
        # `CREATE ... IF NOT EXISTS`, no matching DROP). So a deployment that
        # enabled detection, created the index, then disabled it again can
        # still hit that RESIDUAL index here: a same-(recipient, amount_usd)
        # pending invoice raises a raw sqlite3.IntegrityError out of `_insert`
        # (its own internal catch only recognizes the subscription-renewal
        # index, so this one falls through to `raise`). Fail closed with the
        # same clean, agent-readable refusal the jitter-cap path uses instead
        # of leaking a raw traceback.
        try:
            await _insert(amount_usd)
        except sqlite3.IntegrityError as e:
            if _is_pending_amount_conflict(e):
                raise ValueError(
                    f"a pending agent invoice for ${amount_usd:.2f} to this "
                    "treasury already exists — refusing to create a "
                    "duplicate (same-amount pending-invoice guard); use a "
                    "different amount or wait for the existing invoice to "
                    "resolve"
                ) from e
            raise

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
        "payer_contact": contact,
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
           WHERE (user_id = ? OR json_extract(metadata, '$.tenant_id') = ?)
             AND json_extract(metadata, '$.kind') = ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, user_id, INVOICE_KIND, max(1, int(limit))),
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
            "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
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
        "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


async def get_payment_request_by_tx_hash(transaction_hash: str, *, db=None) -> Optional[Dict[str, Any]]:
    """Read one invoice row by its settlement transaction hash — the
    counterpart to :func:`get_payment_request` for callers that only carry a
    ``transaction_hash`` reference, not the ``request_id`` (Task 15, Phase 4:
    ERC-8004 ``ProofOfPayment`` only carries a ``txHash``, never the invoice's
    own id — see ``modules/eip8004/reputation.py::_verify_payment_proof``).

    ``transaction_hash`` carries a partial UNIQUE index (see
    :func:`settle_payment_request`), so at most one row can ever match — this
    is a safe, unambiguous lookup. Returns ``None`` for a missing/empty hash
    or a non-invoice row (mirrors :func:`get_payment_request`'s
    ``is_invoice_row`` filter); the caller decides what "not found" means
    (typically: refuse to treat the referencing proof as verified)."""
    if not transaction_hash:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE transaction_hash = ?",
        (transaction_hash,))
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
        "transaction_hash": row.get("transaction_hash"),
        "purpose": meta.get("purpose") or "",
        "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


async def get_invoice_tenant(request_id: str, *, db=None) -> Optional[str]:
    """The tenant (user_id) that actually owns this invoice — read directly
    from ITS OWN row (``metadata.tenant_id``, falling back to the ``user_id``
    column for the FK-fallback storage case), independent of anything a
    caller might separately claim.

    Task 14 review Finding 3 (cheap defense-in-depth):
    ``modules.x402.subscriptions.apply_settlement`` uses this to confirm a
    settled invoice's tenant actually matches the subscription it is about to
    extend, before applying the extension — unexploitable today (only the
    settlement watcher ever writes ``metadata.subscription_id``, always
    matching the invoice's own tenant), but cheap insurance against a
    tenant-A invoice silently extending a tenant-B subscription. Returns
    ``None`` for a missing row (permissive — the caller treats "can't
    resolve" as "nothing to compare against", not as a mismatch)."""
    if not request_id:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (request_id,))
    if not row:
        return None
    meta = _row_metadata(row)
    return meta.get("tenant_id") or row.get("user_id") or None


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


async def revert_stale_settling(*, max_age_seconds: int = 600, db=None,
                                ) -> List[Dict[str, Any]]:
    """H7 stale-``settling`` reaper: heal invoices STRANDED in ``settling``.

    A ``claim_for_settlement`` flips ``pending -> settling`` BEFORE the
    facilitator round-trip; ``settle_payment_request`` then flips
    ``settling -> completed``. If the settling task is cancelled/crashes in
    between (client disconnect cancels the request task; a process crash), the
    row is stranded in ``settling`` forever — ``expire_stale_requests`` only
    ever touches ``pending`` rows, and nothing else re-checks ``settling``.

    A genuine settle completes well within the 300s facilitator timeout, so a
    row still ``settling`` past ``max_age_seconds`` (default 600s = 10min) is
    stranded. Revert it to ``pending`` (payable + expirable again) UNLESS it
    already carries a ``transaction_hash`` that ALREADY settled some invoice
    (defensive — a ``settling`` row should never carry one, but never resurrect
    a genuine settle). Returns the reverted invoice dicts so the caller can emit
    an owner notice. The tx-hash uniqueness guard in ``settle_payment_request``
    means a reverted-then-re-paid invoice can never double-settle on-chain."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'settling'
             AND updated_at < datetime('now', ?)
             AND json_extract(metadata, '$.kind') = ?""",
        (f'-{int(max(1, max_age_seconds))} seconds', INVOICE_KIND),
    )
    reverted = []
    for row in rows or []:
        tx = row.get("transaction_hash")
        if tx and await transaction_hash_already_settled(tx, db=database):
            # Genuinely settled elsewhere (should be 'completed', not 'settling')
            # — never resurrect it back to pending.
            continue
        cur = await database.execute(
            "UPDATE x402_payment_requests SET status='pending', updated_at=datetime('now') "
            "WHERE id = ? AND status = 'settling'",
            (row["id"],),
        )
        if not getattr(cur, "rowcount", 0):
            continue  # lost a race to a concurrent settle — leave it
        meta = _row_metadata(row)
        reverted.append({
            "request_id": row["id"],
            "amount_usd": row.get("amount_usd"),
            "session_id": meta.get("session_id") or "",
            "user_id": meta.get("tenant_id") or row.get("user_id") or "",
            "purpose": meta.get("purpose") or "",
        })
    if reverted:
        logger.warning(
            "x402 invoicing: reverted %d invoice(s) stranded in 'settling' past "
            "%ds back to 'pending' (H7 stale-settling reaper): %s",
            len(reverted), max_age_seconds, [r["request_id"] for r in reverted])
    return reverted


async def transaction_hash_already_settled(transaction_hash: str, *, db=None) -> bool:
    """True when `transaction_hash` has already settled SOME invoice (Task 11
    C2 fix). A given on-chain transfer must settle AT MOST ONE invoice EVER:
    without this guard, a settlement-watcher failure that leaves the scan
    checkpoint un-advanced (`SettlementWatcher._scan_onchain` only advances it
    AFTER the full block range is processed) could cause a LATER tick to
    re-process the SAME already-consumed transfer against a DIFFERENT
    (now-unrelated, same-amount) pending invoice — silently redirecting a real
    payer's funds to settle someone else's bill. `transaction_hash` is only
    ever stamped by a successful `settle_payment_request` call, and a
    completed row can never leave that terminal status, so a bare existence
    check is sufficient (no status filter needed)."""
    if not transaction_hash:
        return False
    database = await _resolve_db(db)
    if database is None:
        return False
    row = await database.fetch_one(
        "SELECT 1 AS ok FROM x402_payment_requests WHERE transaction_hash = ? LIMIT 1",
        (transaction_hash,),
    )
    return bool(row)


async def settle_payment_request(
    request_id: str, *, transaction_hash: Optional[str] = None, db=None,
) -> bool:
    """Attested transition to completed (owner CLI / API). Idempotent: only a
    'pending' (owner-attested direct settle) or 'settling' (endpoint post-claim) row
    transitions; anything else returns False. The settlement WAKE + payment_settled
    event are the watcher's job (it runs in the agent process; this may not).

    Task 11 C2 fix: when `transaction_hash` is given, it must never settle a
    SECOND invoice — checked via `transaction_hash_already_settled` BEFORE the
    UPDATE (the primary, always-active guard). The partial UNIQUE index on
    `x402_payment_requests.transaction_hash` (self-healed in
    `X402Tables.create_tables`; also tracked by migration v1.6.0) is
    defense-in-depth against a genuine concurrent-write race this pre-check
    alone can't close — a `sqlite3.IntegrityError` from that race is treated
    the same as "refused", never raised past this function."""
    database = await _resolve_db(db)
    if database is None:
        return False
    if transaction_hash and await transaction_hash_already_settled(
            transaction_hash, db=database):
        logger.warning(
            "x402 settle refused: tx %s already settled a DIFFERENT invoice "
            "(request %s NOT settled — replay guard)", transaction_hash, request_id)
        return False
    try:
        cur = await database.execute(
            """UPDATE x402_payment_requests
               SET status = 'completed', transaction_hash = ?,
                   completed_at = datetime('now'), updated_at = datetime('now')
               WHERE id = ? AND status IN ('pending', 'settling')""",
            (transaction_hash, request_id),
        )
    except sqlite3.IntegrityError:
        # Lost a genuine concurrent race to the UNIQUE index — two settle
        # attempts for the same tx_hash landed together. Never resurrect or
        # half-apply; the row is left untouched.
        logger.warning(
            "x402 settle refused: tx %s hit the transaction_hash uniqueness "
            "guard concurrently (request %s NOT settled)", transaction_hash, request_id)
        return False
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
           WHERE status = 'pending' AND deadline < ?
             AND json_extract(metadata, '$.kind') = ?""",
        (cutoff, INVOICE_KIND),
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
             AND json_extract(metadata, '$.kind') = ? AND metadata LIKE ?""",
        (INVOICE_KIND, '%"wake_delivered": false%'),
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
            "subscription_id": meta.get("subscription_id") or None,
        })
    return out


async def expired_unnotified_invoices(*, db=None) -> List[Dict[str, Any]]:
    """Expired agent invoices whose originating session/owner has not been
    notified yet (G-22 — an unpaid invoice must not silently vanish).

    Mirrors :func:`settled_unnotified_invoices` but reads ``status='expired'``
    rows instead. Reuses the SAME ``wake_delivered`` metadata flag rather than
    a second ``expiry_wake_delivered`` marker: a row is either settled
    (``completed``/``settled_no_tx``) or expired — two MUTUALLY EXCLUSIVE
    terminal states (``expire_stale_requests`` only ever touches
    ``status='pending'`` rows, and a row that has reached one terminal status
    can never transition to the other — see :func:`settle_payment_request` /
    :func:`expire_stale_requests`'s status guards). Since this query and
    :func:`settled_unnotified_invoices` are already partitioned by ``status``,
    one shared flag is sufficient and simpler than two: a settled row can
    never appear here, an expired row can never appear there, so neither
    terminal wake can suppress the other."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'expired'
             AND json_extract(metadata, '$.kind') = ? AND metadata LIKE ?""",
        (INVOICE_KIND, '%"wake_delivered": false%'),
    )
    out = []
    for row in rows or []:
        meta = _row_metadata(row)
        out.append({
            "request_id": row["id"],
            "amount_usd": row.get("amount_usd"),
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


async def claim_expiry_wake(request_id: str, *, db=None) -> bool:
    """Atomically claim the EXPIRY notification for one invoice (G-22).

    Deliberately delegates to :func:`claim_wake` — same atomic CAS over the
    SAME ``wake_delivered`` metadata flag. See :func:`expired_unnotified_invoices`
    for why sharing the flag with settlement is safe (mutually exclusive
    terminal ``status`` values, both watcher queries already status-partitioned).
    Kept as a distinctly-named function — rather than callers reusing
    ``claim_wake`` directly — for call-site clarity/symmetry with
    :func:`claim_wake`, and so the two notification paths can diverge later
    without a shared-name landmine."""
    return await claim_wake(request_id, db=db)


# --- Task 11 (Phase 2): on-chain settlement detection --------------------
# A per-treasury scan checkpoint + an amount-based pending-invoice matcher,
# consumed by `modules.x402.settlement_watcher.SettlementWatcher._scan_onchain`.
# The `settlement_scan` table is created by `modules.database.x402_tables
# .X402Tables.create_tables` (same runtime CREATE-IF-NOT-EXISTS pattern the
# other x402 tables use).

async def get_scan_checkpoint(treasury: str, *, db=None) -> Optional[int]:
    """Last fully-scanned block for this treasury, or None if never scanned."""
    if not treasury:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        "SELECT last_block FROM settlement_scan WHERE treasury = ?", (treasury,))
    if not row or row.get("last_block") is None:
        return None
    return int(row["last_block"])


async def advance_scan_checkpoint(treasury: str, last_block: int, *, db=None) -> None:
    """Persist the new last-scanned block for this treasury. Never regresses
    the stored checkpoint (guards a stray out-of-order/concurrent call)."""
    if not treasury:
        return
    database = await _resolve_db(db)
    if database is None:
        return
    await database.execute(
        """INSERT INTO settlement_scan (treasury, last_block, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(treasury) DO UPDATE SET
               last_block = excluded.last_block, updated_at = excluded.updated_at
           WHERE excluded.last_block > settlement_scan.last_block""",
        (treasury, int(last_block)),
    )


async def match_pending_invoice_by_amount(
    amount_usd: float, treasury: str, *, db=None,
) -> Optional[Dict[str, Any]]:
    """The on-chain settlement-detection ambiguity policy (Task 11): among
    PENDING agent invoices for this treasury at an EXACT amount match, the
    OLDEST (``created_at`` ascending) wins — so one detected transfer settles
    at most one invoice. Not tenant-scoped: an on-chain transfer carries no
    tenant identity, only the recipient (treasury) address; disambiguating
    same-amount invoices is what the amount jitter
    (`x402_invoice_amount_jitter_enabled`) is for.

    Ties on ``created_at`` (SQLite's ``datetime('now')`` is second-precision,
    so two invoices created within the same wall-clock second are common) are
    broken by the table's implicit ``rowid`` — true monotonic insertion order
    — NOT ``id``, which is a random UUID and carries no chronological
    meaning."""
    if not treasury:
        return None
    database = await _resolve_db(db)
    if database is None:
        return None
    row = await database.fetch_one(
        """SELECT * FROM x402_payment_requests
           WHERE status = 'pending' AND recipient = ? AND amount_usd = ?
             AND json_extract(metadata, '$.kind') = ?
           ORDER BY created_at ASC, rowid ASC LIMIT 1""",
        (treasury.lower(), round(float(amount_usd), 6), INVOICE_KIND),
    )
    if not row:
        return None
    meta = _row_metadata(row)
    return {
        "request_id": row["id"],
        "amount_usd": row.get("amount_usd"),
        "session_id": meta.get("session_id") or "",
        "user_id": meta.get("tenant_id") or row.get("user_id") or "",
    }
