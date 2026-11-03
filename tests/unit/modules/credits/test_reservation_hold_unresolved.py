"""098 W1 MO-08: a credit reservation that was never settled (the call raised,
the process died) is recorded as `hold_unresolved` for the admin; it does not
block the user, and only the admin releases or charges it — never a timer."""
import pytest
import pytest_asyncio
from types import SimpleNamespace

from modules.credits import billing_failures as bf
from modules.credits.balance_manager import CreditBalanceManager
from core import billing_context
from core.billing_context import hold_handed_off, record_orphaned_holds
from modules.credits.reservations import credit_reserver
from modules.database.connection import DatabaseConnection


@pytest.fixture(autouse=True)
def _no_open_holds():
    billing_context._OPEN_HOLDS.clear()
    yield
    billing_context._OPEN_HOLDS.clear()


@pytest_asyncio.fixture
async def balance(tmp_path, monkeypatch):
    conn = DatabaseConnection(tmp_path / "credits.db")
    await conn.connect()
    for sql in (
        "CREATE TABLE user_credits (user_id TEXT PRIMARY KEY, balance INTEGER, "
        "lifetime_earned INTEGER DEFAULT 0, lifetime_spent INTEGER DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE credit_transactions (id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, "
        "transaction_type TEXT, reason TEXT, session_id TEXT, balance_before INTEGER, "
        "balance_after INTEGER, timestamp TEXT)",
        "CREATE TABLE billing_failures (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT, "
        "session_id TEXT, request_id TEXT UNIQUE, credits_owed INTEGER, api_cost_usd REAL, "
        "model TEXT, created_at TEXT, resolved_at TEXT, resolution_notes TEXT)",
        "INSERT INTO user_credits (user_id, balance, lifetime_earned) VALUES ('payer', 100, 100)",
    ):
        await conn.execute(sql)
    db = SimpleNamespace(connection=conn, execute=conn.execute,
                         fetch_one=conn.fetch_one, fetch_all=conn.fetch_all)
    monkeypatch.setattr("modules.credits.reservations.reservation_credits", lambda *_: 60)
    await bf.ensure_columns(db)
    yield CreditBalanceManager(db)
    await conn.close()


async def _orphan(balance):
    """A reservation whose call raised before settlement, one hour ago."""
    ref = await credit_reserver(balance, "payer", "s")("model", "provider")
    await balance.db.execute("UPDATE credit_transactions SET timestamp=datetime('now','-2 hours') "
                             "WHERE reason=?", (ref,))
    return ref


@pytest.mark.asyncio
async def test_reaper_records_orphan_reservation_after_1h(balance):
    fresh = await credit_reserver(balance, "payer", "s")("model", "provider")
    hold_handed_off(fresh)   # metered: its settlement is on the way
    await balance.add_credits("payer", 100, "deposit")
    ref = await _orphan(balance)
    assert await bf.reap_open_holds(balance.db) == 1
    assert await bf.reap_open_holds(balance.db) == 0  # once
    rows = await balance.db.fetch_all("SELECT request_id, status FROM billing_failures")
    assert [(r["request_id"], r["status"]) for r in rows] == [(ref, "hold_unresolved")]
    assert fresh != ref


@pytest.mark.asyncio
async def test_hold_unresolved_does_not_block_new_reservation(balance):
    await _orphan(balance)
    await bf.reap_open_holds(balance.db)
    assert await balance.has_sufficient_balance("payer", 10)


@pytest.mark.asyncio
async def test_admin_written_off_releases_whole_hold_once(balance):
    await _orphan(balance)
    await bf.reap_open_holds(balance.db)
    row = await balance.db.fetch_one("SELECT id FROM billing_failures")
    assert (await balance.get_balance("payer"))["balance"] == 40
    await bf.resolve(balance.db, lambda: balance, row["id"], "written_off", admin_id="a")
    assert (await balance.get_balance("payer"))["balance"] == 100
    with pytest.raises(bf.ResolveError):
        await bf.resolve(balance.db, lambda: balance, row["id"], "written_off", admin_id="a")
    assert (await balance.get_balance("payer"))["balance"] == 100


@pytest.mark.asyncio
async def test_admin_charge_of_a_hold_needs_an_amount(balance):
    await _orphan(balance)
    await bf.reap_open_holds(balance.db)
    row = await balance.db.fetch_one("SELECT id FROM billing_failures")
    with pytest.raises(bf.ResolveError) as e:
        await bf.resolve(balance.db, lambda: balance, row["id"], "charged")
    assert e.value.status == 400
    await bf.resolve(balance.db, lambda: balance, row["id"], "charged", amount=25)
    assert (await balance.get_balance("payer"))["balance"] == 75
    st = await balance.db.fetch_one("SELECT status FROM billing_failures")
    assert st["status"] == "charged"


async def _holds(balance):
    rows = await balance.db.fetch_all("SELECT request_id, status, session_id FROM billing_failures")
    return [(r["request_id"], r["status"]) for r in rows]


@pytest.mark.asyncio
async def test_adapter_exception_records_hold_unresolved_at_once(balance):
    """098 close-out MO-08: the call raised inside ``wait_for`` (its own task);
    the step-error handler records the hold at once — no 1 h wait."""
    import asyncio
    reserve = credit_reserver(balance, "payer", "s")
    seen = []

    async def call():
        seen.append(await reserve("model", "provider"))
        raise TimeoutError("provider timed out")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call(), timeout=5)
    assert await record_orphaned_holds() == 1
    assert await _holds(balance) == [(seen[0], "hold_unresolved")]
    assert await record_orphaned_holds() == 0          # once
    assert await balance.has_sufficient_balance("payer", 10)   # never blocks the user
    assert (await balance.get_balance("payer"))["balance"] == 40   # nothing released


@pytest.mark.asyncio
async def test_next_reservation_records_the_previous_orphan(balance):
    await balance.add_credits("payer", 100, "deposit")
    reserve = credit_reserver(balance, "payer", "s")
    first = await reserve("model", "provider")          # its call raised (no hand-off)
    second = await reserve("model", "provider")         # the retry
    assert await _holds(balance) == [(first, "hold_unresolved")]
    hold_handed_off(second)
    await reserve("model", "provider")
    assert await _holds(balance) == [(first, "hold_unresolved")]


@pytest.mark.asyncio
async def test_in_flight_or_settled_holds_are_not_recorded(balance):
    import asyncio
    from modules.credits.reservations import settle_reservation
    await balance.add_credits("payer", 100, "deposit")
    reserve = credit_reserver(balance, "payer", "s")
    settled = await reserve("model", "provider")
    assert await settle_reservation(balance, "payer", settled, 5)
    started, release = asyncio.Event(), asyncio.Event()

    async def sibling():                                # a parallel call still running
        await reserve("model", "provider")
        started.set()
        await release.wait()
    task = asyncio.create_task(sibling())
    await started.wait()
    assert await record_orphaned_holds() == 0
    assert await _holds(balance) == []
    release.set()
    await task
