"""098 W1: migration v1.12.0 adds the x402 invoice-state columns to a legacy
`x402_payment_requests` (MO-02 claim lane, MO-03 refund close-out, MO-06 first
payable block), idempotently, and keeps every legacy row's values."""
import pytest

from migrations.versions.v1_12_0_x402_claim_refund_columns import upgrade, verify, VERSION
from modules.database.connection import DatabaseConnection

_LEGACY = """
CREATE TABLE x402_payment_requests (
    id TEXT PRIMARY KEY, user_id TEXT, payer_address TEXT, amount TEXT NOT NULL,
    amount_usd REAL NOT NULL, asset TEXT NOT NULL, chain TEXT NOT NULL,
    recipient TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL, deadline INTEGER NOT NULL,
    status TEXT DEFAULT 'pending', transaction_hash TEXT, payment_id TEXT, metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
"""
_SCHEMA_VERSIONS = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT DEFAULT 'system', checksum TEXT, execution_time_ms INTEGER)
"""


@pytest.mark.asyncio
async def test_upgrade_adds_columns_to_a_legacy_table_and_keeps_rows(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.execute(_LEGACY)
        await db.execute(_SCHEMA_VERSIONS)
        await db.execute(
            "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,"
            "nonce,deadline,status) VALUES ('old','1',1.0,'usdc','base','0xt','n',1,'settling')")
        assert not await verify(db, None)
        assert await upgrade(db, None)
        assert await upgrade(db, None)  # idempotent
        assert await verify(db, None)
        row = await db.fetch_one("SELECT * FROM x402_payment_requests WHERE id='old'")
        assert row["status"] == "settling" and row["claim_lane"] is None
        assert row["refunded_at"] is None and row["created_block"] is None
        v = await db.fetch_one("SELECT version FROM schema_versions WHERE version=?", (VERSION,))
        assert v is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upgrade_without_the_table_is_a_clean_no_op(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        await db.execute(_SCHEMA_VERSIONS)
        assert await upgrade(db, None)
        assert await verify(db, None)
    finally:
        await db.close()
