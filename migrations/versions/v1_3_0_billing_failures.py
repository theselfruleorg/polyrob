"""
Database Schema Version 1.3.0 - Billing Failures

Tracks billing failures for reconciliation when credits cannot be deducted.
Used by the fail-fast billing system to record unpaid charges.

Created: 2025-12-05
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.3.0"
DESCRIPTION = "Billing Failures - Credit reconciliation tracking"


async def upgrade(db, db_manager):
    """
    Apply v1.3.0 schema - Billing Failures.

    Creates billing_failures table to track unpaid charges
    for admin review and reconciliation.
    """

    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    # Create billing_failures table
    logger.info("Creating billing_failures table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS billing_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            request_id TEXT UNIQUE NOT NULL,
            credits_owed INTEGER NOT NULL,
            api_cost_usd REAL NOT NULL,
            model TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            status TEXT DEFAULT 'pending',
            resolution_notes TEXT
        )
    ''')
    logger.info("  billing_failures table created")

    # Create indexes for efficient queries
    logger.info("Creating indexes...")
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_billing_failures_user
        ON billing_failures(user_id)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_billing_failures_status
        ON billing_failures(status)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_billing_failures_created
        ON billing_failures(created_at)
    ''')
    logger.info("  Indexes created")

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.3.0."""
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DROP INDEX IF EXISTS idx_billing_failures_user")
    await db.execute("DROP INDEX IF EXISTS idx_billing_failures_status")
    await db.execute("DROP INDEX IF EXISTS idx_billing_failures_created")
    await db.execute("DROP TABLE IF EXISTS billing_failures")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        # Check table exists
        tables = await db.fetch_all("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name = 'billing_failures'
        """)

        if len(tables) == 0:
            logger.error("billing_failures table not found")
            return False

        # Check indexes exist
        indexes = await db.fetch_all("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name LIKE 'idx_billing_failures%'
        """)

        if len(indexes) < 3:
            logger.warning(f"Only {len(indexes)} indexes found (expected 3)")

        # Check version is recorded
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
