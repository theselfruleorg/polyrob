"""
Database Schema Version 1.6.0 - x402_payment_requests.transaction_hash
partial unique index (Task 11 review fix C2).

On-chain USDC settlement detection (`modules/x402/settlement_watcher.py::
SettlementWatcher._settle_or_flag`) matches a detected transfer to a PENDING
agent invoice by amount, then settles it and stamps `transaction_hash`. Prior
to this fix there was no per-transfer isolation in that loop AND no DB-level
guarantee that a given on-chain tx could settle only one invoice: a mid-batch
failure -- which left the scan checkpoint un-advanced (the checkpoint only
moves AFTER the full block range is processed) -- could cause a LATER tick to
re-process the SAME already-consumed transfer against a DIFFERENT (now-
unrelated, same-amount) pending invoice, silently redirecting a real payer's
funds to settle someone else's bill.

This migration adds a partial UNIQUE index (NULL-exempt -- pending/expired
rows never carry a transaction_hash, and many may legitimately share NULL) on
`x402_payment_requests.transaction_hash`. The PRIMARY guard is
`modules.x402.invoicing.settle_payment_request`'s explicit
`transaction_hash_already_settled` pre-check (added in the same fix pass);
this index is defense-in-depth against a genuine concurrent-write race that
pre-check alone can't close.

Self-healing note: `modules.database.x402_tables.X402Tables.create_tables()`
ALSO creates this same index on every boot (the runtime CREATE-IF-NOT-EXISTS
pattern the `settlement_scan` table already uses) -- so this migration is
belt-and-suspenders for explicit schema-version tracking, not the sole
application path (mirrors v1.5.0's usage_records.request_id precedent).

Boot-crash-hazard follow-up (Medium, 2026-07-13): a legacy DB may already
contain duplicate non-NULL `transaction_hash` rows (the historical C2 bug
this index guards against), and `CREATE UNIQUE INDEX` over dirty data raises
`sqlite3.IntegrityError` -- which would crash `python -m migrations.migrate
upgrade` outright. This migration and `X402Tables.create_tables()` now share
one helper, `modules.database.x402_tables.dedupe_and_create_tx_hash_unique_index`,
that deduplicates BEFORE creating the index (keeps the earliest row per
duplicated tx_hash, NULLs the tx stamp on the rest -- never deletes rows --
and logs the affected request_ids loudly for reconciliation), and degrades
(logs, does not raise) if the index still can't be created afterward.

Created: 2026-07-13
"""

import logging

from modules.database.x402_tables import dedupe_and_create_tx_hash_unique_index

logger = logging.getLogger(__name__)

VERSION = "1.6.0"
DESCRIPTION = "x402_payment_requests.transaction_hash partial unique index (Task 11 C2)"


async def upgrade(db, db_manager):
    """Apply v1.6.0 schema - x402_payment_requests.transaction_hash unique index.

    Delegates to `dedupe_and_create_tx_hash_unique_index` (shared with
    `X402Tables.create_tables()`), which is tolerant of a DB that doesn't
    have `x402_payment_requests` at all yet (e.g. a minimal/legacy fixture,
    or a boot sequence where this runs before `DatabaseManager`/`X402Tables`
    has created the table) -- `PRAGMA table_info` on a nonexistent table
    returns an empty result rather than raising, so this skips the index
    (nothing to index) instead of crashing boot -- AND deduplicates any
    legacy duplicate `transaction_hash` rows before attempting the index, so
    a dirty pre-existing DB can never IntegrityError-crash this migration
    (see module docstring). The table is self-healed by
    `X402Tables.create_tables()` on every real boot, which ALSO runs this
    same helper -- so a skip/degrade here is never the only application path.
    Idempotent: re-running with the index already present and no duplicate
    data is a no-op.
    """
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    await dedupe_and_create_tx_hash_unique_index(db, logger)

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.6.0 -- drops the index only (never destructive to data)."""
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DROP INDEX IF EXISTS idx_x402_requests_tx_hash_unique")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete (index dropped)")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        index = await db.fetch_one("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_x402_requests_tx_hash_unique'
        """)
        if not index:
            logger.error("idx_x402_requests_tx_hash_unique index missing")
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
