"""F2 (N1 fix): record_x402_payment must actually persist a row.

The original INSERT omitted four NOT NULL columns (amount, recipient, nonce,
deadline), so every insert raised a constraint violation that was swallowed by
the try/except -> the agent settled USDC on-chain and stored zero record.

These tests run against the REAL x402 schema (X402Tables.create_tables) — the
all-fakes test pattern is exactly why N1 shipped.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.x402_tables import X402Tables
from modules.database.user_profiles import UserProfiles
from modules.x402.x402_integration import record_x402_payment


class _FakeContainer:
    def __init__(self, db):
        self._db = db

    def get_service(self, name):
        return self._db if name == "database_manager" else None


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    # x402_payment_requests.user_id has a FK to user_profiles -> create the payer.
    await db.execute(
        "INSERT INTO user_profiles (user_id, wallet_address, role, tier) "
        "VALUES ('usr_1', '0xpayer', 'user', 'x402')"
    )
    return db


def _patch_container(monkeypatch, db):
    from core.container import DependencyContainer

    monkeypatch.setattr(
        DependencyContainer, "get_instance", classmethod(lambda cls, *a, **k: _FakeContainer(db))
    )


@pytest.mark.asyncio
async def test_records_payment_persists_row(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    _patch_container(monkeypatch, db)
    try:
        ok = await record_x402_payment(
            payment_id="x402_abc123",
            wallet_address="0xPAYER",
            user_id="usr_1",
            amount_usd=0.01,
            network="base",
            recipient="0xTREASURY",
            transaction_hash="0xdeadbeef",
            amount_atomic="10000",
        )
        assert ok is True
        row = await db.fetch_one(
            "SELECT * FROM x402_payment_requests WHERE id = 'x402_abc123'"
        )
        assert row is not None, "payment row must be persisted"
        assert row["amount_usd"] == 0.01
        assert row["recipient"] == "0xtreasury"
        assert row["payer_address"] == "0xpayer"
        assert row["user_id"] == "usr_1"
        assert row["status"] == "completed"
        assert row["transaction_hash"] == "0xdeadbeef"
        assert row["nonce"]  # NOT NULL
        assert row["deadline"] is not None  # NOT NULL
        assert row["amount"] == "10000"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_records_payment_when_transaction_missing(tmp_path, monkeypatch):
    """Facilitator can report success with no tx hash; revenue must NOT be dropped."""
    db = await _setup_db(tmp_path)
    _patch_container(monkeypatch, db)
    try:
        ok = await record_x402_payment(
            payment_id="x402_notx",
            wallet_address="0xPAYER",
            user_id="usr_1",
            amount_usd=0.05,
            network="base",
            recipient="0xTREASURY",
            transaction_hash=None,
        )
        assert ok is True
        row = await db.fetch_one(
            "SELECT * FROM x402_payment_requests WHERE id = 'x402_notx'"
        )
        assert row is not None, "tx-less settlement must still record a row"
        assert row["nonce"]  # surrogate, NOT NULL UNIQUE
        assert row["status"] == "settled_no_tx"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_duplicate_nonce_is_idempotent(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    _patch_container(monkeypatch, db)
    try:
        kwargs = dict(
            payment_id="x402_dup",
            wallet_address="0xPAYER",
            user_id="usr_1",
            amount_usd=0.01,
            network="base",
            recipient="0xTREASURY",
            transaction_hash="0xsametx",
        )
        assert await record_x402_payment(**kwargs) is True
        # Replay of the same on-chain tx must not create a second row or raise.
        assert await record_x402_payment(**kwargs) is True
        rows = await db.fetch_all(
            "SELECT id FROM x402_payment_requests WHERE nonce = '0xsametx'"
        )
        assert len(rows) == 1
    finally:
        await db.close()
