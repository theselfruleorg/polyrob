"""F1 (live-test): UserProfiles.create_table must migrate a pre-existing
older-schema table (no `tier` column) instead of crashing on the idx_tier index.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles

# The stale pre-`tier` schema that shipped in older bot.db files.
_OLD_SCHEMA = """
CREATE TABLE user_profiles (
    user_id TEXT PRIMARY KEY,
    is_bot INTEGER DEFAULT 0,
    first_name TEXT,
    last_name TEXT,
    username TEXT,
    language_code TEXT,
    role TEXT DEFAULT 'user',
    preferences TEXT,
    wallet_address TEXT,
    den_password TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


async def _columns(db):
    return {r["name"] for r in await db.fetch_all("PRAGMA table_info(user_profiles)")}


@pytest.mark.asyncio
async def test_create_table_backfills_stale_schema(tmp_path):
    db = DatabaseConnection(tmp_path / "stale.db")
    await db.connect()
    try:
        # Simulate a stale DB: old table WITHOUT the `tier` column.
        await db.execute(_OLD_SCHEMA)
        await db.execute("INSERT INTO user_profiles (user_id) VALUES ('legacy1')")

        # Must NOT raise (previously: 'no such column: tier' on CREATE INDEX idx_tier).
        await UserProfiles(db).create_table()

        cols = await _columns(db)
        assert "tier" in cols
        assert {"email", "current_wallet_chain", "total_sessions"} <= cols
        # Existing row backfilled with the column default.
        row = await db.fetch_one("SELECT tier FROM user_profiles WHERE user_id='legacy1'")
        assert row["tier"] == "free"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_table_idempotent_on_fresh_db(tmp_path):
    db = DatabaseConnection(tmp_path / "fresh.db")
    await db.connect()
    try:
        await UserProfiles(db).create_table()
        # Second call is a no-op (no duplicate-column errors).
        await UserProfiles(db).create_table()
        assert "tier" in await _columns(db)
    finally:
        await db.close()
