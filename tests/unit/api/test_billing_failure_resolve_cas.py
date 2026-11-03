"""098 W1 MO-07: a billing failure is resolved through ONE guarded claim, so an
admin double click charges the user once; an interrupted resolve finishes from
the credit ledger; a refund of nothing is refused."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from modules.credits import billing_failures as bf
from modules.credits.balance_manager import CreditBalanceManager
from modules.database.connection import DatabaseConnection

_TABLES = (
    "CREATE TABLE user_credits (user_id TEXT PRIMARY KEY, balance INTEGER, "
    "lifetime_earned INTEGER DEFAULT 0, lifetime_spent INTEGER DEFAULT 0, updated_at TEXT)",
    "CREATE TABLE credit_transactions (id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, "
    "transaction_type TEXT, reason TEXT, session_id TEXT, balance_before INTEGER, "
    "balance_after INTEGER, timestamp TEXT)",
    "CREATE TABLE billing_failures (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT, "
    "session_id TEXT, request_id TEXT UNIQUE, credits_owed INTEGER, api_cost_usd REAL, "
    "model TEXT, created_at TEXT, resolved_at TEXT, resolution_notes TEXT)",
    "INSERT INTO user_credits (user_id, balance, lifetime_earned) VALUES ('payer', 100, 100)",
    "INSERT INTO billing_failures (id, user_id, status, session_id, request_id, credits_owed, "
    "api_cost_usd, created_at) VALUES (1, 'payer', 'pending', 's', 'req-1', 30, 0.1, 'now')",
)


@pytest_asyncio.fixture
async def rig(tmp_path):
    conn = DatabaseConnection(tmp_path / "bot.db")
    await conn.connect()
    for sql in _TABLES:
        await conn.execute(sql)
    db = SimpleNamespace(connection=conn, execute=conn.execute,
                         fetch_one=conn.fetch_one, fetch_all=conn.fetch_all)
    yield db, CreditBalanceManager(db)
    await conn.close()


async def _status(db):
    return (await db.fetch_one("SELECT status, resolution FROM billing_failures WHERE id=1"))


@pytest.mark.asyncio
async def test_concurrent_charged_resolve_deducts_once(rig):
    db, bal = rig
    out = await asyncio.gather(*(bf.resolve(db, lambda: bal, 1, "charged", admin_id="a")
                                 for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(o, dict) for o in out) == 1
    assert any(isinstance(o, bf.ResolveError) and o.status in (400, 409) for o in out)
    assert (await bal.get_balance("payer"))["balance"] == 70
    assert dict(await _status(db)) == {"status": "charged", "resolution": "charged"}


@pytest.mark.asyncio
async def test_stale_read_loses_the_claim_and_charges_nothing(rig, monkeypatch):
    db, bal = rig
    await bf.resolve(db, lambda: bal, 1, "charged", admin_id="a")
    real = db.fetch_one

    async def stale(sql, params=()):  # the second admin read the row before the first wrote
        row = await real(sql, params)
        if row and "FROM billing_failures WHERE id" in sql:
            row = dict(row, status="pending")
        return row
    db.fetch_one = stale
    with pytest.raises(bf.ResolveError) as e:
        await bf.resolve(db, lambda: bal, 1, "charged", admin_id="b")
    assert e.value.status == 409
    assert (await bal.get_balance("payer"))["balance"] == 70


@pytest.mark.asyncio
async def test_resolving_reaper_finds_committed_charge(rig):
    db, bal = rig
    await bf.ensure_columns(db)
    # crash after the claim and the deduction, before the terminal write
    await db.execute("UPDATE billing_failures SET status='resolving', resolution='from:pending', "
                     "resolving_at=1 WHERE id=1")
    await bal.deduct_credits("payer", 30, f"{bf.REASON_PREFIX}1")
    assert await bf.reap_resolving(db) == 1
    assert (await _status(db))["status"] == "charged"
    # nothing moved -> back to pending
    await db.execute("INSERT INTO billing_failures (id, user_id, status, session_id, request_id, "
                     "credits_owed, api_cost_usd, created_at, resolution, resolving_at) VALUES "
                     "(2, 'payer', 'resolving', 's', 'req-2', 5, 0, 'now', 'from:pending', 1)")
    assert await bf.reap_resolving(db) == 1
    row = await db.fetch_one("SELECT status FROM billing_failures WHERE id=2")
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_one_charge_index_refuses_a_second_deduction(rig):
    db, bal = rig
    await bf.ensure_columns(db)
    assert await bal.deduct_credits("payer", 30, f"{bf.REASON_PREFIX}1")
    with pytest.raises(Exception):
        await bal.deduct_credits("payer", 30, f"{bf.REASON_PREFIX}1")
    assert (await bal.get_balance("payer"))["balance"] == 70


@pytest.mark.asyncio
async def test_refunded_on_unreserved_row_is_rejected(rig):
    db, bal = rig
    with pytest.raises(bf.ResolveError) as e:
        await bf.resolve(db, lambda: bal, 1, "refunded")
    assert e.value.status == 400 and "written_off" in e.value.detail
    assert (await _status(db))["status"] == "pending"


@pytest.mark.asyncio
async def test_resolving_row_blocks_new_reservations(rig):
    db, bal = rig
    await bf.ensure_columns(db)
    await db.execute("UPDATE billing_failures SET status='resolving', resolution='from:pending' "
                     "WHERE id=1")
    assert not await bal.has_sufficient_balance("payer", 1)


@pytest.mark.asyncio
async def test_resolution_column_backfilled(rig):
    db, _ = rig
    from migrations.versions.v1_13_0_billing_failure_states import upgrade, verify
    await db.execute("CREATE TABLE schema_versions (id INTEGER PRIMARY KEY, version TEXT UNIQUE, "
                     "description TEXT, applied_at TEXT)")
    await db.execute("UPDATE billing_failures SET status='resolved', "
                     "resolution_notes='written_off: loss (by a)' WHERE id=1")
    await db.execute("INSERT INTO billing_failures (id, user_id, status, session_id, request_id, "
                     "credits_owed, api_cost_usd, created_at, resolution_notes) VALUES "
                     "(3, 'payer', 'resolved', 's', 'req-3', 5, 0, 'now', 'odd note')")
    assert not await verify(db, None)
    assert await upgrade(db, None) and await upgrade(db, None)
    assert await verify(db, None)
    assert dict(await _status(db)) == {"status": "written_off", "resolution": "written_off"}
    row = await db.fetch_one("SELECT status FROM billing_failures WHERE id=3")
    assert row["status"] == "resolved"
    assert not bf.BILLING_FAILURE.validate()
