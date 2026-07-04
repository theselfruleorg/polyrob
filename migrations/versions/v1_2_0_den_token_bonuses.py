"""
Database Schema Version 1.2.0 - DEN Token Bonuses

Simple table to track which token IDs have been used for bonus.
Each token ID can only grant bonus ONCE - we don't track WHO used it.

Created: 2025-12-04
"""

import logging

logger = logging.getLogger(__name__)

VERSION = "1.2.0"
DESCRIPTION = "DEN Token Bonuses - One-time per Token ID"


async def upgrade(db, db_manager):
    """
    Apply v1.2.0 schema - DEN Token Bonuses.

    Simple table: just track if a token ID has been used.
    """

    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    # Simple table - just token_id + contract_address
    logger.info("Creating den_token_bonuses table...")
    await db.execute('''
        CREATE TABLE IF NOT EXISTS den_token_bonuses (
            token_id TEXT NOT NULL,
            contract_address TEXT NOT NULL,
            bonus_granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (token_id, contract_address)
        )
    ''')
    logger.info("  den_token_bonuses table created")

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.2.0."""
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DROP TABLE IF EXISTS den_token_bonuses")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly."""

    try:
        # Check table exists
        tables = await db.fetch_all("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name = 'den_token_bonuses'
        """)

        if len(tables) == 0:
            logger.error("den_token_bonuses table not found")
            return False

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
