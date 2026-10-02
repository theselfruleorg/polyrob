"""
Database Schema Version 1.10.0 - usage_records.cache_creation_tokens (F17)

`calculate_cost` has billed cache-WRITE tokens separately since G3, and
`record_llm_usage` has taken a `cache_creation_tokens` argument for just as
long -- but the number reached the cost arithmetic and then vanished. The
`usage_records` row stored `input_tokens`, `output_tokens` and `cached_tokens`
(cache READS) only, so nothing after the fact could say how much of a session's
input was a cache WRITE. That is the number that decides whether a cache is
paying for itself: a write costs MORE than an uncached token (1.25x on
Anthropic's 5-minute window, 2x on the 1-hour one), so a prefix that is written
and never read again is a loss, and the existing `cache_ratio` reads exactly the
same whether that happened or not.

This migration adds the real column. It mirrors
`modules/database/auth_tables.py::create_tables` (the inline creator, which
self-heals the SAME column onto a pre-existing table on every boot) and
`modules/database/schema.sql` -- all three must agree, and
`tests/unit/migrations/test_inline_schema_at_head.py` enforces that.

Created: 2026-09-22
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.10.0"
DESCRIPTION = "usage_records.cache_creation_tokens column (F17 cache-write visibility)"

_COLUMN = "cache_creation_tokens"


async def upgrade(db, db_manager):
    """Apply v1.10.0 schema - usage_records.cache_creation_tokens.

    Idempotent: SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and
    the inline creator may already have self-healed the column in the same boot,
    so check `PRAGMA table_info` first and tolerate a duplicate-column error.
    """
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    existing_cols = await db.fetch_all("PRAGMA table_info(usage_records)")
    if _COLUMN not in {c["name"] for c in existing_cols}:
        logger.info(f"  Adding usage_records.{_COLUMN} column...")
        try:
            await db.execute(
                f"ALTER TABLE usage_records ADD COLUMN {_COLUMN} INTEGER DEFAULT 0"
            )
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
    else:
        logger.info(f"  usage_records.{_COLUMN} already present (skipping ALTER)")

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.10.0.

    SQLite `DROP COLUMN` needs a full table rebuild on the versions this project
    supports, and rebuilding a billing table to remove an additive, defaulted
    column would risk far more than it removes. The column is left in place
    (zeros everywhere going forward, which is what a pre-F17 reader assumed
    anyway); only the version stamp is withdrawn.
    """
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete ({_COLUMN} column "
                f"intentionally left in place -- SQLite DROP COLUMN requires a "
                f"table rebuild of a billing table)")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        cols = await db.fetch_all("PRAGMA table_info(usage_records)")
        if _COLUMN not in {c["name"] for c in cols}:
            logger.error(f"usage_records.{_COLUMN} column missing")
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
