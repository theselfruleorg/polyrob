"""085 L-5: add_credits joins only a transaction that ITS OWN task opened.

The old check (`not in_transaction()`) was True for ANY task's open transaction,
so add_credits then ran its UPDATE and INSERT outside a transaction of its own:
each statement waited for the other task and then auto-committed alone. A
failure between them left a credit with no ledger row.
"""
import asyncio

import pytest

from modules.credits.balance_manager import CreditBalanceManager
from modules.database.connection import DatabaseConnection


class _DB:
    def __init__(self, conn):
        self.connection = conn

    async def execute(self, sql, params=()):
        return await self.connection.execute(sql, params)

    async def fetch_one(self, sql, params=()):
        return await self.connection.fetch_one(sql, params)


async def _make_db(tmp_path, *, ledger_ok=True):
    conn = DatabaseConnection(tmp_path / "credits.db")
    await conn.connect()
    await conn.execute(
        "CREATE TABLE user_credits (user_id TEXT PRIMARY KEY, balance INTEGER DEFAULT 0, "
        "lifetime_earned INTEGER DEFAULT 0, lifetime_spent INTEGER DEFAULT 0, "
        "updated_at TEXT)")
    if ledger_ok:
        await conn.execute(
            "CREATE TABLE credit_transactions (user_id TEXT, amount INTEGER, "
            "transaction_type TEXT, reason TEXT, balance_before INTEGER, "
            "balance_after INTEGER, timestamp TEXT)")
    else:
        # A ledger table the INSERT cannot write: the failure between the
        # balance UPDATE and the ledger row.
        await conn.execute("CREATE TABLE credit_transactions (user_id TEXT)")
    await conn.execute("CREATE TABLE other (x INTEGER)")
    return _DB(conn)


async def _balance(db, user):
    row = await db.fetch_one("SELECT balance FROM user_credits WHERE user_id = ?", (user,))
    return row["balance"] if row else None


def test_credit_is_atomic_while_another_task_holds_a_transaction(tmp_path):
    async def run():
        db = await _make_db(tmp_path, ledger_ok=False)
        await db.execute("INSERT INTO user_credits (user_id, balance) VALUES ('u1', 0)")
        mgr = CreditBalanceManager(db)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def other_task():
            await db.connection.begin_transaction()
            await db.execute("INSERT INTO other (x) VALUES (1)")
            holding.set()
            await release.wait()
            await db.connection.commit()

        other = asyncio.create_task(other_task())
        await holding.wait()
        credit = asyncio.create_task(mgr.add_credits("u1", 100, "grant", "admin_grant"))
        await asyncio.sleep(0.05)
        release.set()
        await other
        with pytest.raises(Exception):
            await credit
        # The ledger INSERT failed, so our OWN transaction rolled the UPDATE back.
        assert await _balance(db, "u1") == 0
        assert not await db.connection.in_transaction()
    asyncio.run(run())


def test_credit_waits_for_the_other_task_and_then_lands_whole(tmp_path):
    async def run():
        db = await _make_db(tmp_path)
        await db.execute("INSERT INTO user_credits (user_id, balance) VALUES ('u1', 0)")
        mgr = CreditBalanceManager(db)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def other_task():
            await db.connection.begin_transaction()
            await db.execute("INSERT INTO other (x) VALUES (1)")
            holding.set()
            await release.wait()
            await db.connection.rollback()  # the other task's rollback

        other = asyncio.create_task(other_task())
        await holding.wait()
        credit = asyncio.create_task(mgr.add_credits("u1", 100, "grant", "admin_grant"))
        await asyncio.sleep(0.05)
        release.set()
        await other
        assert await credit is True
        assert await _balance(db, "u1") == 100
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM credit_transactions")
        assert row["n"] == 1
    asyncio.run(run())


def test_same_task_outer_transaction_is_joined_not_nested(tmp_path):
    """The DepositMonitor shape: one outer transaction holds credit + mark."""
    async def run():
        db = await _make_db(tmp_path)
        mgr = CreditBalanceManager(db)
        await db.connection.begin_transaction()
        assert await mgr.add_credits("u1", 50, "deposit", "purchase") is True
        assert await db.connection.in_transaction()  # not ended early
        await db.connection.rollback()
        # The caller's rollback covers the credit too.
        assert await _balance(db, "u1") is None
    asyncio.run(run())


def test_begin_never_waits_forever_on_an_abandoned_transaction(tmp_path):
    """An owner task that ended without commit/rollback never sets _txn_done:
    our BEGIN clears the abandoned span (H8 rolls it back) instead of hanging."""
    from modules.credits.balance_manager import _begin_own_transaction

    async def run():
        db = await _make_db(tmp_path)

        async def abandon():
            await db.connection.begin_transaction()
            await db.execute("INSERT INTO other (x) VALUES (1)")

        await asyncio.create_task(abandon())
        async with asyncio.timeout(2):  # same task: the BEGIN must be ours
            await _begin_own_transaction(db.connection)
        assert db.connection._txn_owner is asyncio.current_task()
        await db.connection.commit()
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM other")
        assert row["n"] == 0  # the dead task's partial write was rolled back
    asyncio.run(run())
