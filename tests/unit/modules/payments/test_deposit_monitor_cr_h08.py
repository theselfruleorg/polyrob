"""CR-H08: the deposit monitor credits only the INCREASE over the last
observed balance per (user, chain, token), never the whole balance again.

Uses a real sqlite database so the mark table's schema, upsert and the
pre-CR-H08 baseline fallback run for real.
"""
import sqlite3

import pytest

from modules.payments.deposit_monitor import DepositMonitor


class _Conn:
    def __init__(self, db):
        self._db = db

    async def begin_transaction(self):
        self._db.raw.execute("SAVEPOINT tx")

    async def commit(self):
        self._db.raw.execute("RELEASE tx")

    async def rollback(self):
        self._db.raw.execute("ROLLBACK TO tx")
        self._db.raw.execute("RELEASE tx")


class _SqliteDB:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:", isolation_level=None)
        self.raw.row_factory = sqlite3.Row
        self.raw.execute("""
            CREATE TABLE crypto_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, chain TEXT,
                deposit_address TEXT, token_symbol TEXT, amount TEXT, amount_usd REAL,
                credits_purchased INTEGER, status TEXT, detected_at TIMESTAMP)
        """)
        self.connection = _Conn(self)

    async def execute(self, sql, params=()):
        self.raw.execute(sql, params or ())

    async def fetch_one(self, sql, params=()):
        row = self.raw.execute(sql, params or ()).fetchone()
        return dict(row) if row else None


class _Ledger:
    def __init__(self):
        self.credits = 0

    async def add_credits(self, user_id, amount, reason):
        self.credits += amount
        return True


class _Config:
    deposit_check_interval = 60


def _usdc(balance):
    return {"chain": "ethereum", "token_symbol": "USDC", "amount": str(balance), "amount_usd": balance}


@pytest.mark.asyncio
async def test_growing_balance_credits_only_the_increase():
    db, ledger = _SqliteDB(), _Ledger()
    m = DepositMonitor(db, ledger, _Config())
    for bal in (10.0, 10.5, 11.0, 16.0):
        await m._process_deposit("u", _usdc(bal))
    # 10 credited, 0.5 and 0.5 are dust below $5 and accumulate, 16 - 10 = 6 credited.
    assert ledger.credits == 1600, ledger.credits


@pytest.mark.asyncio
async def test_same_balance_never_credits_twice_and_sweep_lowers_the_mark():
    db, ledger = _SqliteDB(), _Ledger()
    m = DepositMonitor(db, ledger, _Config())
    await m._process_deposit("u", _usdc(60.0))
    await m._process_deposit("u", _usdc(60.0))
    assert ledger.credits == 6000
    await m._process_deposit("u", _usdc(0.0))   # swept
    assert ledger.credits == 6000
    await m._process_deposit("u", _usdc(20.0))  # a new deposit after the sweep
    assert ledger.credits == 8000


@pytest.mark.asyncio
async def test_pre_mark_database_uses_last_credited_balance_as_baseline():
    db, ledger = _SqliteDB(), _Ledger()
    # A row written by the old code: the whole balance reading in `amount`.
    db.raw.execute(
        "INSERT INTO crypto_payments (user_id, chain, token_symbol, amount, amount_usd,"
        " credits_purchased, status) VALUES ('u','ethereum','USDC','11.0',11.0,1100,'confirmed')")
    m = DepositMonitor(db, ledger, _Config())
    await m._process_deposit("u", _usdc(11.0))
    assert ledger.credits == 0
    await m._process_deposit("u", _usdc(21.0))
    assert ledger.credits == 1000


@pytest.mark.asyncio
async def test_marks_table_creation_is_idempotent():
    db = _SqliteDB()
    for _ in range(2):
        m = DepositMonitor(db, _Ledger(), _Config())
        await m._ensure_marks_table()
    await m._process_deposit("u", _usdc(10.0))
    row = db.raw.execute("SELECT balance FROM deposit_balance_marks").fetchone()
    assert row["balance"] == "10.0"


@pytest.mark.asyncio
async def test_decimal_credit_floor_does_not_lose_one_credit():
    db, ledger = _SqliteDB(), _Ledger()
    monitor = DepositMonitor(db, ledger, _Config())
    await monitor._process_deposit("u", _usdc(5.1))
    assert ledger.credits == 510
    row = db.raw.execute("SELECT credits_purchased FROM crypto_payments").fetchone()
    assert row["credits_purchased"] == 510


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_balance", ["NaN", "Infinity", "-Infinity", "-1"])
async def test_invalid_balance_never_lowers_mark_or_credits(bad_balance):
    db, ledger = _SqliteDB(), _Ledger()
    monitor = DepositMonitor(db, ledger, _Config())
    await monitor._process_deposit("u", _usdc(10))
    await monitor._process_deposit("u", _usdc(bad_balance))
    assert ledger.credits == 1000
    row = db.raw.execute("SELECT balance FROM deposit_balance_marks").fetchone()
    assert row["balance"] == "10"
