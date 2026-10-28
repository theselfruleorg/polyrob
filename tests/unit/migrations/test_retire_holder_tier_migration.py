"""v1.11.0: the NFT `holder` tier is retired.

A legacy db (the old CHECK allows 'holder', the two ownership columns exist) has
its holder rows moved to `free_access`; a db without `user_profiles` is a no-op;
the inline creator no longer creates the ownership columns or allows 'holder'.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles

_LEGACY_USER_PROFILES = """
CREATE TABLE user_profiles (
    user_id TEXT PRIMARY KEY,
    wallet_address TEXT UNIQUE NOT NULL,
    role TEXT DEFAULT 'user' NOT NULL,
    tier TEXT DEFAULT 'free' NOT NULL,
    den_token_count INTEGER DEFAULT 0,
    den_token_verified_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (tier IN ('free', 'free_access', 'holder', 'x402', 'admin'))
)
"""

_SCHEMA_VERSIONS = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.mark.asyncio
async def test_holder_rows_move_to_free_access(tmp_path):
    from migrations.versions.v1_11_0_retire_holder_tier import upgrade, verify

    db = DatabaseConnection(tmp_path / "legacy.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USER_PROFILES)
        await db.execute(_SCHEMA_VERSIONS)
        for uid, tier in (("a", "holder"), ("b", "free"), ("c", "admin")):
            await db.execute(
                "INSERT INTO user_profiles (user_id, wallet_address, tier) VALUES (?, ?, ?)",
                (uid, f"0x{uid * 40}", tier))
        assert await verify(db, None) is False

        assert await upgrade(db, None) is True
        rows = {r["user_id"]: r["tier"] for r in
                await db.fetch_all("SELECT user_id, tier FROM user_profiles")}
        assert rows == {"a": "free_access", "b": "free", "c": "admin"}
        assert await verify(db, None) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_user_profiles_table_is_a_no_op(tmp_path):
    from migrations.versions.v1_11_0_retire_holder_tier import upgrade, verify

    db = DatabaseConnection(tmp_path / "bare.db")
    await db.connect()
    try:
        await db.execute(_SCHEMA_VERSIONS)
        assert await upgrade(db, None) is True
        assert await verify(db, None) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_table_has_no_ownership_columns_and_refuses_holder(tmp_path):
    db = DatabaseConnection(tmp_path / "fresh.db")
    await db.connect()
    try:
        await UserProfiles(db).create_table()
        cols = {r["name"] for r in await db.fetch_all("PRAGMA table_info(user_profiles)")}
        assert not {"den_token_count", "den_token_verified_at"} & cols
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO user_profiles (user_id, wallet_address, tier) "
                "VALUES ('x', '0x1', 'holder')")
    finally:
        await db.close()
