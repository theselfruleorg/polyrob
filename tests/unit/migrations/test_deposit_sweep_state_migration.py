"""098 close-out MO-01: migration v1.15.0 adds the sweep-state columns to an
existing `crypto_payments` (a swept row reads `swept`, the rest `unswept`), is
idempotent, and is a no-op when the table does not exist yet."""
import pytest

from migrations.versions.v1_15_0_deposit_sweep_state import upgrade, verify
from modules.database.connection import DatabaseConnection

_SV = ("CREATE TABLE IF NOT EXISTS schema_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
       "version TEXT NOT NULL UNIQUE, description TEXT NOT NULL, applied_at TIMESTAMP, "
       "applied_by TEXT DEFAULT 'system', checksum TEXT, execution_time_ms INTEGER)")


@pytest.mark.asyncio
async def test_upgrade_adds_state_and_reads_old_sweeps_as_swept(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.execute(_SV)
        await db.execute("CREATE TABLE crypto_payments (id INTEGER PRIMARY KEY, user_id TEXT, "
                         "chain TEXT, deposit_address TEXT, tx_hash TEXT, token_symbol TEXT, "
                         "amount TEXT, amount_usd REAL, credits_purchased INTEGER, status TEXT, "
                         "detected_at TIMESTAMP, swept_at TIMESTAMP, sweep_tx_hash TEXT)")
        await db.execute("INSERT INTO crypto_payments (id, user_id, chain, token_symbol, amount, "
                         "amount_usd, credits_purchased, status, swept_at) VALUES "
                         "(1,'u','ethereum','USDC','10',10,1000,'confirmed','2026-01-01'),"
                         "(2,'u','ethereum','USDC','5',5,500,'confirmed',NULL)")
        assert not await verify(db, None)
        assert await upgrade(db, None) and await upgrade(db, None)
        assert await verify(db, None)
        rows = await db.fetch_all("SELECT id, sweep_state FROM crypto_payments ORDER BY id")
        assert [(r["id"], r["sweep_state"]) for r in rows] == [(1, "swept"), (2, "unswept")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_without_the_table_is_a_noop(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.execute(_SV)
        assert await upgrade(db, None) and await verify(db, None)
    finally:
        await db.close()
