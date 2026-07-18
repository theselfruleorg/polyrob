"""
Database Schema Version 1.5.0 - usage_records.request_id (G-26)

`record_llm_usage` (modules/credits/usage_tracker.py) generates a UUID
"for deduplication" but historically stored it ONLY inside the `metadata`
JSON blob -- nothing at the DB level actually prevented a retry with a
fresh response object from double-writing the same charge as a second row
(the only dedup was the in-process `_polyrob_billed` flag on the response
object, which a retry naturally doesn't carry).

This migration adds a REAL `request_id` column plus a partial UNIQUE index
(NULL-exempt, so legacy rows and any caller that genuinely has no
request_id are unaffected) so a duplicate write is rejected at the DB
level, not silently duplicated.

Tolerant of already-applied: `modules/database/auth_tables.py::create_tables`
self-heals the SAME column onto a pre-existing `usage_records` table on every
boot (idempotent ALTER, same idiom as auth_nonces/chain_id), so by the time
this migration runs the column may already exist -- both the ALTER and the
index creation below are guarded/idempotent.

HONESTY NOTE: credit DEDUCTION happens BEFORE this write
(usage_tracker.py `record_llm_usage`, `_deduct_from_balance` runs ahead of
`_write_to_database`), so this unique index prevents duplicate ROWS/ledger
inflation in `usage_records`, NOT duplicate deduction -- the in-process
`_polyrob_billed` guard on the response object remains the deduction-side
dedup. This migration does not (and cannot) change that ordering.

Created: 2026-07-13
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.5.0"
DESCRIPTION = "usage_records.request_id column + partial unique index (G-26 dedup)"


async def upgrade(db, db_manager):
    """Apply v1.5.0 schema - usage_records.request_id.

    Adds a real `request_id TEXT` column to `usage_records` (idempotent: SQLite
    has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so we check
    `PRAGMA table_info` first) plus a partial UNIQUE index so a duplicate
    request_id can never produce a second row.
    """
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    existing_cols = await db.fetch_all("PRAGMA table_info(usage_records)")
    if "request_id" not in {c["name"] for c in existing_cols}:
        logger.info("  Adding usage_records.request_id column...")
        try:
            await db.execute("ALTER TABLE usage_records ADD COLUMN request_id TEXT")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
    else:
        logger.info("  usage_records.request_id already present (skipping ALTER)")

    logger.info("  Creating idx_usage_records_request_id (partial unique)...")
    await db.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_records_request_id
        ON usage_records(request_id) WHERE request_id IS NOT NULL
    ''')

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.5.0.

    SQLite can't DROP COLUMN on older versions without a full table rebuild;
    dropping just the index is enough to remove the dedup constraint (the
    column staying around with NULLs everywhere going forward is harmless).
    """
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DROP INDEX IF EXISTS idx_usage_records_request_id")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete (index dropped; "
                f"request_id column intentionally left in place -- SQLite "
                f"DROP COLUMN requires a table rebuild)")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        cols = await db.fetch_all("PRAGMA table_info(usage_records)")
        if "request_id" not in {c["name"] for c in cols}:
            logger.error("usage_records.request_id column missing")
            return False

        index = await db.fetch_one("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_usage_records_request_id'
        """)
        if not index:
            logger.error("idx_usage_records_request_id index missing")
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
