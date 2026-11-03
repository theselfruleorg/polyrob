"""098 W1 MO-01: the sweeper books what it moved on the deposit mark at the
sweep's block; the monitor moves the mark by CAS on the value it read and never
credits from a reading older than the last booked sweep."""
from decimal import Decimal

import pytest

from modules.payments.deposit_monitor import DepositMonitor, book_sweep_outflow
from tests.unit.modules.payments.test_deposit_monitor_cr_h08 import _Config, _Ledger, _SqliteDB


def _usdc(balance, block):
    return {"chain": "ethereum", "token_symbol": "USDC", "amount": str(balance),
            "amount_usd": balance, "block": block}


async def _mark(db, token="USDC"):
    return await db.fetch_one("SELECT balance, as_of_block FROM deposit_balance_marks "
                              "WHERE user_id='u' AND chain='ethereum' AND token_symbol=?",
                              (token,))


@pytest.mark.asyncio
async def test_deposit_after_sweep_before_poll_is_credited():
    db, ledger = _SqliteDB(), _Ledger()
    m = DepositMonitor(db, ledger, _Config())
    await m._process_deposit("u", _usdc(100.0, 10))
    assert ledger.credits == 10000
    assert await book_sweep_outflow(db, "u", "ethereum", "USDC", Decimal("100"), 20)
    await m._process_deposit("u", _usdc(30.0, 25))     # deposited after the sweep
    assert ledger.credits == 13000


@pytest.mark.asyncio
async def test_sweep_of_uncredited_deposit_is_credited_once():
    db, ledger = _SqliteDB(), _Ledger()
    m = DepositMonitor(db, ledger, _Config())
    await m._process_deposit("u", _usdc(100.0, 10))
    # 30 more landed (not yet polled); the sweep moved all 130
    assert await book_sweep_outflow(db, "u", "ethereum", "USDC", Decimal("130"), 20)
    await m._process_deposit("u", _usdc(0.0, 25))
    assert ledger.credits == 13000
    await m._process_deposit("u", _usdc(0.0, 26))
    assert ledger.credits == 13000


@pytest.mark.asyncio
async def test_stale_reading_below_mark_block_credits_nothing():
    db, ledger = _SqliteDB(), _Ledger()
    m = DepositMonitor(db, ledger, _Config())
    await m._process_deposit("u", _usdc(100.0, 10))
    await book_sweep_outflow(db, "u", "ethereum", "USDC", Decimal("100"), 20)
    before = dict(await _mark(db))
    await m._process_deposit("u", _usdc(100.0, 15))     # a reading from before the sweep
    assert ledger.credits == 10000 and dict(await _mark(db)) == before


@pytest.mark.asyncio
async def test_token_sweep_gas_lowers_eth_mark():
    db, ledger = _SqliteDB(), _Ledger()
    m = DepositMonitor(db, ledger, _Config())
    await m._ensure_marks_table()
    db.raw.execute("INSERT INTO deposit_balance_marks (user_id, chain, token_symbol, balance, "
                   "as_of_block) VALUES ('u','ethereum','ETH','1000000',5)")
    assert await book_sweep_outflow(db, "u", "ethereum", "ETH", 400000, 20)
    row = await _mark(db, "ETH")
    assert Decimal(row["balance"]) == 600000 and row["as_of_block"] == 20


@pytest.mark.asyncio
async def test_concurrent_monitors_credit_once():
    db, ledger = _SqliteDB(), _Ledger()
    m1, m2 = DepositMonitor(db, ledger, _Config()), DepositMonitor(db, ledger, _Config())
    await m1._process_deposit("u", _usdc(50.0, 10))
    real = m2._mark_row

    async def stale(*key):   # m2 read the mark before m1's credit landed
        return None
    m2._mark_row = stale
    await m2._process_deposit("u", _usdc(50.0, 10))
    m2._mark_row = real
    assert ledger.credits == 5000
