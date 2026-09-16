"""Amount-collision jitter for on-chain settlement matching.

Extracted from `invoicing.py` (046 Phase 0) under the repo's decomposition rule.
One cohesive concern: keeping two PENDING invoices for the same treasury, the
same asset and the same producer from carrying the SAME payable amount, because
the EVM settlement pass identifies an invoice BY that amount.

Three layers, each closing a window the one above it cannot:

1. a per-treasury in-process lock around the dedupe SELECT + INSERT (the
   same-process TOCTOU);
2. a nudged candidate amount when a collision is found;
3. a partial UNIQUE index as the CROSS-process backstop under ``workers>1``.

⚠️ Since 046 all three are keyed on ``(recipient, asset_address, amount_raw)``.
Without the asset in the key, two different tokens at the same USD price collide
for no reason — and, worse, the uniqueness on-chain matching depends on would be
asserted ACROSS assets rather than within one.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Dict, Optional

from modules.x402.invoice_assets import INVOICE_KIND, atomic_amount

logger = logging.getLogger(__name__)



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
_PENDING_AMOUNT_INDEX = "idx_x402_requests_pending_amount_asset_unique"
#: 046 phase 2: the SAME guarantee for a PINNED-raw producer (a room action).
#: Created UNCONDITIONALLY on that path, not only when jitter/detection is on: a
#: room action is identified BY its amount always, so two pending offers sharing
#: one would settle the oldest — the wrong payer's, against the wrong target.
_PENDING_RAW_INDEX = "idx_x402_requests_pending_raw_unique"
#: The pre-046 index. Never dropped, so it can still fire on a deployment
#: that created it; `_is_pending_amount_conflict` recognises both.
_LEGACY_PENDING_AMOUNT_INDEX = "idx_x402_requests_pending_amount_unique"


def _is_pending_amount_conflict(err: Exception) -> bool:
    """True when an ``IntegrityError`` is the M5 pending-amount unique
    violation. SQLite reports a UNIQUE index on plain COLUMNS by the column
    names — NOT the index name (that form is reserved for indexes on
    expressions, like the subscription index ``json_extract(...)``), so match
    on the ``(recipient, amount_usd)`` column signature."""
    msg = str(err)
    if "UNIQUE constraint failed" not in msg or "recipient" not in msg:
        return False
    # 046: both index shapes are recognised. A deployment that ran the pre-046
    # jitter path still carries the residual `(recipient, amount_usd)` index
    # (self-healing CREATE IF NOT EXISTS, no matching DROP), and an
    # unrecognised conflict would surface as a raw traceback.
    return "amount_usd" in msg or "amount_raw" in msg


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
        # 046: keyed on the ASSET and the RAW amount. Without the asset in the
        # key, two different tokens at the same USD price collide for no reason,
        # and — worse — the uniqueness on-chain matching depends on would be
        # asserted ACROSS assets rather than within one.
        await database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_PENDING_AMOUNT_INDEX} "
            "ON x402_payment_requests(recipient, asset_address, amount_raw) "
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


async def _ensure_pending_raw_unique_index(database) -> None:
    """The pinned-raw partial UNIQUE index (046 phase 2).

    Scoped to the kinds that PIN their raw amount — today ``room_action``. The
    agent-invoice index above is kept separate because its creation is
    deliberately conditional on the jitter path, and a room action's uniqueness
    may not depend on a flag.

    ⚠️ Residual window, stated rather than hidden: because the two indexes are
    per-kind, a room offer and an agent invoice could still race to the same raw
    amount ACROSS processes. The SELECT in `_dedupe_raw_for_treasury` spans every
    payable kind and the per-treasury lock serializes it, which covers POLYROB's
    documented `UVICORN_WORKERS=1` model. One index over both kinds would close
    it — and would also start enforcing uniqueness on agent invoices in a
    deployment that merely enabled rooms, refusing a legitimate second invoice at
    the same amount. That trade is not worth taking silently.
    """
    try:
        await database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_PENDING_RAW_INDEX} "
            "ON x402_payment_requests(recipient, asset_address, amount_raw) "
            "WHERE status = 'pending' "
            "AND json_extract(metadata, '$.kind') = 'room_action'"
        )
    except Exception as e:
        logger.warning(
            "x402 invoicing: could not create %s (likely pre-existing duplicate "
            "same-amount pending room-action rows) — degrading to the "
            "in-process per-treasury lock: %s", _PENDING_RAW_INDEX, e)



#: Below this many decimals an asset is stablecoin-scale and keeps the legacy
#: one-raw-unit nudge. At or above it, one raw unit is dust nobody can type.
_ROUND_STEP_MIN_DECIMALS = 12


def jitter_step(decimals) -> int:
    """How far apart two pending amounts of this asset must sit.

    ⚠️ Live 2026-09-15: a 3,500 PNL offer quoted ``SEND EXACTLY
    3500.000000000000000005 PNL``. The step was ONE raw unit — a millionth of a
    dollar in 6-decimal USDC, harmless; 5e-18 in an 18-decimal token, a figure
    no wallet renders and no payer can reproduce, while settlement compares the
    integer EXACTLY.

    So the step scales: a whole token for a memecoin-scale asset (3500 -> 3501,
    still round and still unique), one raw unit for a stablecoin (unchanged —
    widening USDC would move a real price by a visible cent for no reason).

    Never zero: a zero step would loop forever looking for a free amount.
    """
    try:
        d = int(decimals)
    except (TypeError, ValueError):
        return 1
    if d < _ROUND_STEP_MIN_DECIMALS:
        return 1
    return 10 ** d

async def _dedupe_raw_for_treasury(
    raw: int, recipient: str, database, *, asset_address: Optional[str] = None,
    decimals: Optional[int] = None,
) -> int:
    """046 phase 2: a UNIQUE pending ``amount_raw`` for this treasury + asset.

    ⚠️ The USD jitter below could not do this job for a PINNED-raw invoice, and
    looked as though it did. `create_payment_request` writes
    ``final_raw = int(amount_raw)`` whenever the caller pins one, so the jitter
    moved only the DISPLAYED dollars — while `_dedupe_amount_for_treasury`
    probed `atomic_amount(candidate)`, a raw value that would never be inserted,
    and therefore reported "no collision" on its first try, every time.

    The step is ONE raw unit (a millionth of a dollar in USDC): invisible to a
    payer reading a full-precision token amount, and the settlement match is an
    exact integer compare, so one unit is all the separation it needs.

    ⚠️ Spans EVERY payable kind, not just the caller's. The matcher
    (`invoice_assets.match_pending_invoice`) is kind-blind, so the uniqueness it
    depends on must be too — a $0.50 agent invoice and a $0.50 room offer in the
    same asset collided for exactly that reason.
    """
    from modules.x402.invoice_assets import _payable_kinds
    kinds = _payable_kinds()
    placeholders = ",".join("?" for _ in kinds)
    addr = (asset_address or "").strip().lower()
    step = jitter_step(decimals if decimals is not None else 6)
    for i in range(500):
        candidate = int(raw) + i * step
        row = await database.fetch_one(
            f"""SELECT COUNT(*) AS n FROM x402_payment_requests
               WHERE status = 'pending' AND recipient = ?
                 AND amount_raw = ?
                 AND lower(COALESCE(asset_address, '')) = ?
                 AND json_extract(metadata, '$.kind') IN ({placeholders})""",
            (recipient, str(candidate), addr, *kinds),
        )
        if not row or not int(row.get("n") or 0):
            return candidate
    raise ValueError(
        "could not find a unique pending amount for this treasury after 500 "
        "raw steps (too many same-amount pending invoices)")


async def insert_with_unique_raw(insert_fn, raw: int, recipient: str, database,
                                 *, asset_address: Optional[str] = None,
                                 decimals: Optional[int] = None) -> int:
    """Insert a PINNED-raw invoice at an amount no other pending row holds.

    The whole concern in one place — the index, the per-treasury lock, the
    nudge and the cross-process IntegrityError retry — so `invoicing.py` keeps
    one call instead of the loop (the decomposition rule its size ratchet
    enforces).

    ``insert_fn(raw)`` must perform the INSERT at exactly *raw*. Returns the raw
    that was actually written, which the caller needs: it is what the offer text
    quotes and what the settlement match compares.
    """
    await _ensure_pending_raw_unique_index(database)
    async with _treasury_lock(recipient):
        attempts = 0
        while True:
            raw = await _dedupe_raw_for_treasury(
                raw, recipient, database, asset_address=asset_address,
                decimals=decimals)
            try:
                await insert_fn(raw)
                return raw
            except sqlite3.IntegrityError as e:
                # A concurrent worker took this raw between our SELECT and our
                # INSERT. Step past it, exactly as the USD path does.
                if not _is_pending_amount_conflict(e):
                    raise
                attempts += 1
                if attempts > 100:
                    raise ValueError(
                        "could not find a unique pending amount for this "
                        "treasury after 100 raw steps") from e
                raw += 1


async def _dedupe_amount_for_treasury(
    amount_usd: float, recipient: str, cap: float, database, *,
    asset_address: Optional[str] = None, decimals: int = 6,
    kind: str = INVOICE_KIND,
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
        # 046: scoped to ONE asset. A ROB invoice and a USDC invoice at the
        # same price are already unambiguous on-chain (different contracts), so
        # perturbing one because of the other moves a quoted price for nothing.
        #
        # ⚠️ Phase 2 dropped the PRODUCER predicate. The settlement matcher is
        # kind-blind (`match_pending_invoice` takes every payable kind), so a
        # per-producer uniqueness check left an agent invoice and a room offer
        # at the same price in the same asset colliding — and the oldest wins.
        from modules.x402.invoice_assets import _payable_kinds
        kinds = _payable_kinds()
        placeholders = ",".join("?" for _ in kinds)
        row = await database.fetch_one(
            f"""SELECT COUNT(*) AS n FROM x402_payment_requests
               WHERE status = 'pending' AND recipient = ?
                 AND amount_raw = ?
                 AND lower(COALESCE(asset_address, '')) = ?
                 AND json_extract(metadata, '$.kind') IN ({placeholders})""",
            (recipient, str(atomic_amount(candidate, decimals)),
             (asset_address or "").strip().lower(), *kinds),
        )
        if not row or not int(row.get("n") or 0):
            return candidate
    return amount_usd
