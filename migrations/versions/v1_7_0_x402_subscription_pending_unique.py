"""
Database Schema Version 1.7.0 - x402_payment_requests pending-subscription
partial unique index (Task 14 review fix, Important — duplicate-renewal
TOCTOU).

Watchtower subscriptions (`modules/x402/subscriptions.py`) create a renewal
invoice ahead of a subscription's `paid_through` from the settlement-watcher
tick (`SettlementWatcher._request_or_create_renewal` ->
`_create_renewal_invoice` -> `modules.x402.invoicing.create_payment_request`).
Before creating one, `subscriptions._has_open_renewal_invoice` runs a plain
SELECT to check "is there already a pending renewal invoice for this
subscription?" -- but that SELECT and the later INSERT are two separate
awaited DB calls with no atomic guard between them. Under
`UVICORN_WORKERS>1`, two concurrent settlement-watcher processes could both
observe "no open invoice" in the same tick window and both create a renewal
invoice for the SAME subscription; if both later settle,
`modules.x402.subscriptions.apply_settlement` would extend `paid_through` by
TWO periods instead of one (a real revenue-accounting bug, not just a
duplicate row).

This migration adds a partial UNIQUE index (NULL/absent-subscription_id-
exempt -- most invoices never carry a subscription_id at all, and plenty may
legitimately share NULL there) on
`json_extract(x402_payment_requests.metadata, '$.subscription_id')`, scoped to
`status = 'pending'` rows only. `modules.x402.invoicing.create_payment_request`
now catches the resulting `sqlite3.IntegrityError` and raises a `ValueError`
("a pending renewal invoice already exists...") instead of propagating the
raw DB error -- the same "refused, not crashed" contract `subscriptions.
_create_renewal_invoice` already handles for other refusal reasons (cap/
config). The plain SELECT in `_has_open_renewal_invoice` stays as the
fast-path check (avoids the round-trip cost of racing the INSERT every tick);
this index is the atomic backstop that actually closes the TOCTOU.

Self-healing note: `modules.database.x402_tables.X402Tables.create_tables()`
ALSO creates this same index on every boot via the shared helper
`dedupe_and_create_subscription_pending_unique_index` (the runtime
CREATE-IF-NOT-EXISTS pattern the `settlement_scan` table and the v1.6.0
tx_hash index already use) -- so this migration is belt-and-suspenders for
explicit schema-version tracking, not the sole application path (mirrors
v1.6.0's own precedent).

Boot-crash-hazard follow-up (same shape as v1.6.0, addressed up front rather
than as a follow-up): a legacy DB may already contain more than one PENDING
invoice sharing the same `metadata.subscription_id` -- exactly the race this
index guards against -- and `CREATE UNIQUE INDEX` over dirty data raises
`sqlite3.IntegrityError`, which would crash `python -m migrations.migrate
upgrade` outright. This migration and `X402Tables.create_tables()` share one
helper, `dedupe_and_create_subscription_pending_unique_index`, that
deduplicates BEFORE creating the index (keeps the earliest pending row per
subscription_id, NULLs the JSON `subscription_id` field on the rest via
`json_set(...)` -- never deletes or re-statuses rows -- and logs the affected
request_ids loudly for reconciliation), and degrades (logs, does not raise)
if the index still can't be created afterward.

Created: 2026-07-13
"""

import logging

from modules.database.x402_tables import dedupe_and_create_subscription_pending_unique_index

logger = logging.getLogger(__name__)

VERSION = "1.7.0"
DESCRIPTION = "x402_payment_requests pending-subscription partial unique index (Task 14 duplicate-renewal TOCTOU)"


async def upgrade(db, db_manager):
    """Apply v1.7.0 schema - x402_payment_requests pending-subscription unique index.

    Delegates to `dedupe_and_create_subscription_pending_unique_index` (shared
    with `X402Tables.create_tables()`), which is tolerant of a DB that doesn't
    have `x402_payment_requests` at all yet -- `PRAGMA table_info` on a
    nonexistent table returns an empty result rather than raising, so this
    skips the index (nothing to index) instead of crashing boot -- AND
    deduplicates any legacy duplicate-pending-subscription rows before
    attempting the index (see module docstring). The table is self-healed by
    `X402Tables.create_tables()` on every real boot, which ALSO runs this same
    helper -- so a skip/degrade here is never the only application path.
    Idempotent: re-running with the index already present and no duplicate
    data is a no-op.
    """
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    await dedupe_and_create_subscription_pending_unique_index(db, logger)

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.7.0 -- drops the index only (never destructive to data)."""
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DROP INDEX IF EXISTS idx_x402_requests_pending_subscription_unique")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete (index dropped)")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        index = await db.fetch_one("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_x402_requests_pending_subscription_unique'
        """)
        if not index:
            logger.error("idx_x402_requests_pending_subscription_unique index missing")
            return False

        result = await db.fetch_one("""
            SELECT version FROM schema_versions WHERE version = ?
        """, (VERSION,))
        if not result:
            logger.error("Version not recorded in schema_versions")
            return False

        logger.info(f"Verification passed for {VERSION}")
        return True

    except Exception as e:
        logger.error(f"Verification error for {VERSION}: {e}")
        return False
