"""
Database Schema Version 1.4.0 - Polymarket Credentials

Adds support for blockchain wallet credentials and trading limits
for the Polymarket MCP integration.

Created: 2025-12-29
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.4.0"
DESCRIPTION = "Polymarket Credentials - Wallet config and trading limits"


async def upgrade(db, db_manager):
    """
    Apply v1.4.0 schema - Polymarket Credentials.

    Creates polymarket_credentials table for encrypted wallet data
    and trading limits configuration.
    """

    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    # Create polymarket_credentials table
    logger.info("Creating polymarket_credentials table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS polymarket_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,

            -- Encrypted wallet credentials
            wallet_address TEXT,
            private_key_encrypted BLOB,

            -- Mode configuration
            demo_mode INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,

            -- Trading limits (JSON)
            trading_limits TEXT DEFAULT '{}',

            -- Connection status
            last_connected_at TEXT,
            last_error TEXT,
            connection_count INTEGER DEFAULT 0,

            -- Timestamps
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')
    logger.info("  polymarket_credentials table created")

    # Create polymarket_audit_log table for trading activity
    logger.info("Creating polymarket_audit_log table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS polymarket_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            tool_name TEXT,
            market_id TEXT,
            details TEXT,
            ip_address TEXT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')
    logger.info("  polymarket_audit_log table created")

    # Create indexes
    logger.info("Creating indexes...")
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_polymarket_credentials_user
        ON polymarket_credentials(user_id)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_polymarket_audit_user
        ON polymarket_audit_log(user_id)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_polymarket_audit_timestamp
        ON polymarket_audit_log(timestamp)
    ''')
    await db.execute('''
        CREATE INDEX IF NOT EXISTS idx_polymarket_audit_action
        ON polymarket_audit_log(action)
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
    """Downgrade from v1.4.0."""
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DROP INDEX IF EXISTS idx_polymarket_credentials_user")
    await db.execute("DROP INDEX IF EXISTS idx_polymarket_audit_user")
    await db.execute("DROP INDEX IF EXISTS idx_polymarket_audit_timestamp")
    await db.execute("DROP INDEX IF EXISTS idx_polymarket_audit_action")
    await db.execute("DROP TABLE IF EXISTS polymarket_credentials")
    await db.execute("DROP TABLE IF EXISTS polymarket_audit_log")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        # Check tables exist
        tables = await db.fetch_all("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ('polymarket_credentials', 'polymarket_audit_log')
        """)

        if len(tables) < 2:
            logger.error(f"Expected 2 tables, found {len(tables)}")
            return False

        # Check indexes exist
        indexes = await db.fetch_all("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name LIKE 'idx_polymarket%'
        """)

        if len(indexes) < 4:
            logger.warning(f"Only {len(indexes)} indexes found (expected 4)")

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
