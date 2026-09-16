"""043 A20 — x402 machine payments (A2A / /v1 billed routes) settle under the
payer's derived ``usr_<hex>`` id with no ``metadata``, so the unified ledger's
tenant predicate (``user_id = ? OR json_extract(metadata, '$.tenant_id') = ?``,
``modules/credits/unified_ledger.py``) matches nothing and the owner's
``/finance``/``/status`` render $0.00 over real machine income.

``record_x402_payment`` gains ``tenant_id`` and writes a ``metadata`` column so
the row is queryable under the OWNER tenant (not just the payer's own derived
id), the same way agent invoices already are.
"""
import asyncio
import json

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables


def _metadata(row):
    """`DatabaseConnection.fetch_*` auto-parses JSON-looking TEXT columns into a
    dict; a raw sqlite row would give the str. Tolerant of both, matching
    ``modules/x402/invoicing.py::_row_metadata``."""
    meta = row["metadata"]
    return meta if isinstance(meta, dict) else json.loads(meta)


@pytest.fixture()
def bot_db(tmp_path):
    async def _setup():
        db = DatabaseConnection(tmp_path / "bot.db")
        await db.connect()
        await UserProfiles(db).create_table()
        await X402Tables(db).create_tables()
        # x402_payment_requests.user_id has an FK to user_profiles -> create
        # the payer profile first (mirrors test_record_payment.py's pattern),
        # otherwise the INSERT fails FK enforcement and no row is written.
        await db.execute(
            "INSERT INTO user_profiles (user_id, wallet_address, role, tier) "
            "VALUES ('usr_abc123', '0xpayer', 'user', 'x402')"
        )
        return db

    db = asyncio.run(_setup())
    yield db
    asyncio.run(db.close())


def test_machine_payment_counts_as_owner_income(bot_db):
    from modules.x402.x402_integration import record_x402_payment
    from modules.credits.unified_ledger import build_ledger

    ok = asyncio.run(record_x402_payment(
        payment_id="pay-1", wallet_address="0xpayer", user_id="usr_abc123",
        amount_usd=0.25, network="base", recipient="0xTREASURY",
        transaction_hash="0xtx", tenant_id="owner-1", db=bot_db))
    assert ok is True

    ledger = asyncio.run(build_ledger("owner-1", days=7, db=bot_db))
    assert ledger["treasury"]["income_usd"] == pytest.approx(0.25)

    row = asyncio.run(bot_db.fetch_one(
        "SELECT metadata FROM x402_payment_requests WHERE id='pay-1'"))
    meta = _metadata(row)
    assert meta["tenant_id"] == "owner-1"
    assert meta["payer_user_id"] == "usr_abc123"


def test_machine_payment_with_explicit_empty_tenant_is_honest_not_owned(bot_db):
    """A caller-supplied empty ``tenant_id`` (e.g. a test, or a future non-
    middleware caller) must not silently attach to the payer's OWN id under
    some other tenant's ledger — the row is simply unattributed. Note: since
    the fix-round-1 change, the PRODUCTION middleware call site now always
    resolves a non-empty tenant via ``resolve_owner_user_id`` (it falls back
    to the local tenant, never ``None``/``""``) — this path is exercised only
    when a caller passes ``tenant_id=""`` explicitly, not as the middleware's
    everyday behavior."""
    from modules.x402.x402_integration import record_x402_payment

    ok = asyncio.run(record_x402_payment(
        payment_id="pay-2", wallet_address="0xpayer", user_id="usr_abc123",
        amount_usd=0.10, network="base", recipient="0xTREASURY",
        transaction_hash="0xtx2", tenant_id="", db=bot_db))
    assert ok is True

    row = asyncio.run(bot_db.fetch_one(
        "SELECT metadata FROM x402_payment_requests WHERE id='pay-2'"))
    meta = _metadata(row)
    assert meta["tenant_id"] == ""
    assert meta["payer_user_id"] == "usr_abc123"


def test_record_x402_payment_still_works_without_tenant_id(bot_db):
    """Back-compat: existing callers that don't pass tenant_id/db keep working
    (tenant_id defaults to None -> stored as "" in metadata)."""
    from modules.x402.x402_integration import record_x402_payment

    ok = asyncio.run(record_x402_payment(
        payment_id="pay-3", wallet_address="0xpayer", user_id="usr_abc123",
        amount_usd=0.05, network="base", recipient="0xTREASURY",
        transaction_hash="0xtx3", db=bot_db))
    assert ok is True

    row = asyncio.run(bot_db.fetch_one(
        "SELECT metadata FROM x402_payment_requests WHERE id='pay-3'"))
    meta = _metadata(row)
    assert meta["kind"] == "machine_payment"
    assert meta["tenant_id"] == ""
    assert meta["payer_user_id"] == "usr_abc123"


def test_resolve_owner_user_id_ranks_local_owner_above_the_unbound_default(monkeypatch):
    """Fix round 1: the console's ledger read (webview/webgate.py::local_owner_id)
    ranks POLYROB_LOCAL_OWNER ABOVE the unbound default. A bare
    resolve_owner_principal() does not consult POLYROB_LOCAL_OWNER at all, so on
    a deploy that sets it without POLYROB_OWNER_USER_ID, the middleware would
    stamp a different tenant than the console reads — the unified-ledger
    predicate misses and A20 is not actually closed.
    core.instance.resolve_owner_user_id is now the ONE home for this
    precedence, and webview.webgate.local_owner_id delegates to it.

    ⚠️ 2026-09-15: that unbound default is the LOCAL tenant, not the instance id.
    The x402 machine-income row is therefore stamped with the tenant the REPL,
    goals and memory already use — which is the whole point of stamping it."""
    from core.instance import resolve_owner_user_id
    from webview.webgate import local_owner_id

    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("BOT_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("SURFACE_SUPER_ADMIN_USER_IDS", raising=False)
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)

    monkeypatch.setenv("POLYROB_LOCAL_OWNER", "alice")
    assert resolve_owner_user_id() == "alice"
    assert resolve_owner_user_id() == local_owner_id()

    monkeypatch.delenv("POLYROB_LOCAL_OWNER", raising=False)
    assert resolve_owner_user_id() == "local"  # LocalIdentity.USER_ID
    assert resolve_owner_user_id() == local_owner_id()
