"""098 W1 MO-01: migration v1.14.0 adds `as_of_block` to an existing
`deposit_balance_marks` (keeping each mark) and creates the table when absent."""
import pytest

from migrations.versions.v1_14_0_deposit_marks_block import upgrade, verify
from modules.database.connection import DatabaseConnection

_SV = ("CREATE TABLE IF NOT EXISTS schema_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
       "version TEXT NOT NULL UNIQUE, description TEXT NOT NULL, applied_at TIMESTAMP, "
       "applied_by TEXT DEFAULT 'system', checksum TEXT, execution_time_ms INTEGER)")


@pytest.mark.asyncio
async def test_upgrade_adds_block_and_keeps_marks(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.execute(_SV)
        await db.execute("CREATE TABLE deposit_balance_marks (user_id TEXT NOT NULL, chain TEXT "
                         "NOT NULL, token_symbol TEXT NOT NULL, balance TEXT NOT NULL, updated_at "
                         "TIMESTAMP, PRIMARY KEY (user_id, chain, token_symbol))")
        await db.execute("INSERT INTO deposit_balance_marks VALUES ('u','ethereum','USDC','42',NULL)")
        assert not await verify(db, None)
        assert await upgrade(db, None) and await upgrade(db, None)
        assert await verify(db, None)
        row = await db.fetch_one("SELECT balance, as_of_block FROM deposit_balance_marks")
        assert (row["balance"], row["as_of_block"]) == ("42", 0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_creates_the_table(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.execute(_SV)
        assert await upgrade(db, None) and await verify(db, None)
    finally:
        await db.close()
