"""CR-M18: paying a renewal invoice never reactivates a CANCELED
subscription, and a cancel expires its open renewal invoice."""
import time

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing, subscriptions as subs


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")


async def _db(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


async def _sub(db):
    return await subs.create_subscription(
        user_id="rob", correspondent_surface="email",
        correspondent_address="a@x.com", cron_job_id="job1",
        period_days=30, paid_through=int(time.time()), db=db)


@pytest.mark.asyncio
async def test_settling_a_renewal_of_a_canceled_subscription_is_refused(tmp_path):
    db = await _db(tmp_path)
    try:
        sub = await _sub(db)
        before = (await subs.get_subscription(sub["id"], db=db))["paid_through"]
        await db.execute("UPDATE subscriptions SET status='canceled' WHERE id=?", (sub["id"],))
        result = await subs.apply_settlement(sub["id"], "req_late", db=db)
        assert result == subs.SettlementResult.REFUSED
        row = await subs.get_subscription(sub["id"], db=db)
        assert row["status"] == subs.STATUS_CANCELED
        assert row["paid_through"] == before
        ledger = await db.fetch_one(
            "SELECT 1 AS ok FROM subscription_applied_settlements WHERE request_id='req_late'")
        assert ledger is None, "a refused settlement left a ledger row"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_expires_the_pending_renewal_invoice(tmp_path):
    db = await _db(tmp_path)
    try:
        sub = await _sub(db)
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="", amount_usd=sub["amount_usd"],
            purpose="renewal", subscription_id=sub["id"], db=db)
        rid = inv["request_id"] if isinstance(inv, dict) else inv
        assert await subs.cancel_subscription(sub["id"], user_id="rob", db=db) is True
        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
        assert row["status"] == "expired"
        assert await invoicing.expired_unnotified_invoices(db=db) == []
    finally:
        await db.close()
