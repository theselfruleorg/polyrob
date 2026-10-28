"""
Database Schema Version 1.11.0 - retire the NFT `holder` tier

The `holder` tier was granted by an NFT-ownership check against one
deployment's token contract. That gate is deleted: a tier is now only
`free`, `free_access` (admin-granted), `x402` or `admin`.

A row still at `holder` would read as an invalid tier, so this migration moves
it to `free_access` -- the user keeps the access they had, and an admin can
revoke it from the users page. The count is logged.

The old `den_token_count` / `den_token_verified_at` columns are left in place
on an existing table (SQLite `DROP COLUMN` needs a table rebuild on the
versions this project supports); nothing reads or writes them any more, and
the inline creator (`modules/database/user_profiles.py`) no longer creates
them on a fresh install.
"""

import logging

logger = logging.getLogger(__name__)


async def _holder_count(db) -> int:
    """Rows still at `holder`; 0 when the table does not exist (a db that never
    had the auth tables)."""
    table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_profiles'")
    if not table:
        return 0
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM user_profiles WHERE tier = 'holder'")
    return (row or {}).get("n", 0) or 0


VERSION = "1.11.0"
DESCRIPTION = "Retire the NFT holder tier (holder -> free_access)"


async def upgrade(db, db_manager):
    """Apply v1.11.0 - move every `holder` row to `free_access`."""
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    moved = await _holder_count(db)
    if moved:
        await db.execute(
            "UPDATE user_profiles SET tier = 'free_access', "
            "updated_at = CURRENT_TIMESTAMP WHERE tier = 'holder'")
    logger.info(f"  {moved} holder row(s) moved to free_access")

    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.11.0.

    The moved rows are not moved back: nothing can tell a former holder from an
    admin grant, and no code grants `holder` any more. Only the stamp is withdrawn.
    """
    logger.info(f"Downgrading schema: {VERSION}")
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))
    logger.info(f"Schema {VERSION} downgrade complete")
    return True


async def verify(db, db_manager):
    """Verify no row is left at the retired tier."""
    try:
        left = await _holder_count(db)
        if left:
            logger.error(f"{left} user_profiles row(s) still at tier 'holder'")
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
