"""046 Phase 0: the four asset columns, added idempotently."""
import logging
import sqlite3

import pytest

from modules.database.x402_tables import add_payment_asset_columns

_ASSET_COLS = {"asset_id", "asset_address", "asset_decimals", "amount_raw"}


class _SqliteDB:
    """The `db.execute`/`db.fetch_all` shape migrations/ and modules/database use."""

    def __init__(self, conn):
        self.conn = conn

    async def execute(self, sql, params=()):
        self.conn.execute(sql, params)
        self.conn.commit()

    async def fetch_all(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    async def fetch_one(self, sql, params=()):
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None


@pytest.fixture()
def legacy_db():
    """The PRE-046 table shape, so the migration is exercised for real."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE x402_payment_requests (
        id TEXT PRIMARY KEY, user_id TEXT, amount TEXT NOT NULL,
        amount_usd REAL NOT NULL, asset TEXT NOT NULL, chain TEXT NOT NULL,
        recipient TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL,
        deadline INTEGER NOT NULL, status TEXT DEFAULT 'pending',
        metadata TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)""")
    conn.commit()
    return _SqliteDB(conn)


async def _cols(db):
    return {r["name"] for r in await db.fetch_all(
        "PRAGMA table_info(x402_payment_requests)")}


@pytest.mark.asyncio
async def test_the_four_columns_are_added(legacy_db):
    await add_payment_asset_columns(legacy_db, logging.getLogger())
    assert _ASSET_COLS <= await _cols(legacy_db)


@pytest.mark.asyncio
async def test_amount_raw_is_TEXT_not_INTEGER(legacy_db):
    """⚠️ An 18-decimal amount exceeds SQLite's signed 64-bit INTEGER range; an
    INTEGER column would overflow silently on a large-supply token."""
    await add_payment_asset_columns(legacy_db, logging.getLogger())
    rows = await legacy_db.fetch_all("PRAGMA table_info(x402_payment_requests)")
    by_name = {r["name"]: r["type"] for r in rows}
    assert by_name["amount_raw"] == "TEXT"
    assert by_name["asset_decimals"] == "INTEGER"


@pytest.mark.asyncio
async def test_it_is_idempotent(legacy_db):
    log = logging.getLogger()
    await add_payment_asset_columns(legacy_db, log)
    await add_payment_asset_columns(legacy_db, log)
    assert _ASSET_COLS <= await _cols(legacy_db)


@pytest.mark.asyncio
async def test_existing_rows_survive_with_null_asset_columns(legacy_db):
    await legacy_db.execute(
        "INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,"
        "recipient,nonce,deadline,status,metadata) "
        "VALUES('inv_x','1.0',1.0,'usdc','base','0xabc','n1',9,'pending','{}')")
    await add_payment_asset_columns(legacy_db, logging.getLogger())
    row = await legacy_db.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id='inv_x'")
    assert row["asset"] == "usdc"
    assert row["asset_id"] is None
    assert row["amount_raw"] is None


@pytest.mark.asyncio
async def test_a_missing_table_is_skipped_not_crashed():
    """Boot must degrade, never crash, on a DB that has no x402 table yet."""
    empty = _SqliteDB(sqlite3.connect(":memory:"))
    await add_payment_asset_columns(empty, logging.getLogger())


@pytest.mark.asyncio
async def test_a_fresh_install_has_the_columns_without_any_migration(x402_db):
    """⚠️ create_tables() must add them too, or a fresh install is missing what
    an upgraded install has."""
    rows = await x402_db.fetch_all("PRAGMA table_info(x402_payment_requests)")
    assert _ASSET_COLS <= {r["name"] for r in rows}
