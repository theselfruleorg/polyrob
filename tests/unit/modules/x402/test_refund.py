"""F11 (N4): x402 settles BEFORE the downstream runs; a 5xx must flag refund_due.

Without this, a customer pays and gets a server error with no record to refund
from. We can't auto-refund via the facilitator here, but we mark the (now always
recorded — see F2) payment row refund_due for reconciliation.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.x402_tables import X402Tables
from modules.database.user_profiles import UserProfiles
from modules.x402.x402_integration import (
    record_x402_payment, mark_payment_refund_due, should_refund_on_status,
)


@pytest.mark.parametrize("status,expected", [
    (200, False), (201, False), (302, False), (402, False), (404, False),
    (499, False), (500, True), (502, True), (503, True), (504, True),
])
def test_should_refund_on_status(status, expected):
    assert should_refund_on_status(status) is expected


class _FakeContainer:
    def __init__(self, db):
        self._db = db

    def get_service(self, name):
        return self._db if name == "database_manager" else None


@pytest.mark.asyncio
async def test_mark_payment_refund_due_updates_row(tmp_path, monkeypatch):
    db = DatabaseConnection(tmp_path / "x.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    await db.execute(
        "INSERT INTO user_profiles (user_id, wallet_address, role, tier) "
        "VALUES ('usr_1','0xp','user','x402')"
    )
    from core.container import DependencyContainer
    monkeypatch.setattr(
        DependencyContainer, "get_instance",
        classmethod(lambda cls, *a, **k: _FakeContainer(db)),
    )
    try:
        await record_x402_payment(
            payment_id="x402_r", wallet_address="0xP", user_id="usr_1",
            amount_usd=0.01, network="base", recipient="0xT", transaction_hash="0xtx",
        )
        ok = await mark_payment_refund_due("x402_r")
        assert ok is True
        row = await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id='x402_r'")
        assert row["status"] == "refund_due"
    finally:
        await db.close()
