"""098 W1 MO-05: every subscription move is a CAS on status AND paid_through;
a reactivation buys a period from now; a subscription whose cron job ended, or
one suspended for 90 days, is canceled (its renewal invoice expires)."""
import json
import time

import pytest

from modules.x402 import subscriptions as subs


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")


async def _sub(db, paid_through, status="active"):
    sub = await subs.create_subscription(
        user_id="alice", correspondent_surface="email", correspondent_address="a@x.com",
        cron_job_id="job1", period_days=30, paid_through=paid_through, db=db)
    if status != "active":
        await db.execute("UPDATE subscriptions SET status=? WHERE id=?", (status, sub["id"]))
    return sub["id"]


@pytest.mark.asyncio
async def test_reactivation_from_suspended_starts_period_now(x402_db):
    sid = await _sub(x402_db, int(time.time()) - 60 * 86400, status="suspended")
    before = int(time.time())
    assert await subs.apply_settlement(sid, "req_1", db=x402_db) == subs.SettlementResult.APPLIED
    row = await subs.get_subscription(sid, db=x402_db)
    assert row["status"] == "active" and row["paid_through"] >= before + 30 * 86400 - 1


@pytest.mark.asyncio
async def test_grace_cas_skips_row_extended_between_select_and_update(x402_db):
    now = time.time()
    sid = await _sub(x402_db, int(now) - 10)
    real = x402_db.fetch_all

    async def stale(sql, params=()):
        rows = await real(sql, params)
        if "FROM subscriptions WHERE status = ?" in sql:
            # a renewal settled between this read and the grace write
            await x402_db.execute("UPDATE subscriptions SET paid_through=? WHERE id=?",
                                  (int(now) + 30 * 86400, sid))
        return rows
    x402_db.fetch_all = stale
    assert await subs.subscriptions_to_grace(now=now, db=x402_db) == []
    x402_db.fetch_all = real
    assert (await subs.get_subscription(sid, db=x402_db))["status"] == "active"


@pytest.mark.asyncio
async def test_cancelled_cron_job_cancels_subscription_and_expires_renewal(x402_db):
    sid = await _sub(x402_db, int(time.time()) + 86400)
    await x402_db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES ('ren1','1',1.0,'usdc','base','0xt','ren1',"
        "9999999999,'pending',?)", (json.dumps({"subscription_id": sid,
                                                 "wake_delivered": False}),))
    out = await subs.reap_subscriptions(db=x402_db, job_status=lambda jid: "cancelled")
    assert [s["id"] for s in out] == [sid]
    assert (await subs.get_subscription(sid, db=x402_db))["status"] == "canceled"
    row = await x402_db.fetch_one("SELECT status FROM x402_payment_requests WHERE id='ren1'")
    assert row["status"] == "expired"
    # a job the store does not know is NOT proof: nothing is canceled
    sid2 = await _sub(x402_db, int(time.time()) + 86400)
    assert await subs.reap_subscriptions(db=x402_db, job_status=lambda j: "missing") == []
    assert (await subs.get_subscription(sid2, db=x402_db))["status"] == "active"


@pytest.mark.asyncio
async def test_suspended_older_than_90d_is_cancelled(x402_db):
    sid = await _sub(x402_db, int(time.time()) - 100 * 86400, status="suspended")
    out = await subs.reap_subscriptions(db=x402_db, job_status=lambda j: "scheduled")
    assert [(s["id"], s["reason"]) for s in out] == [(sid, "suspended_too_long")]
    assert not subs.SUBSCRIPTION.validate()


@pytest.mark.asyncio
async def test_owner_reactivate_loses_to_a_concurrent_move(x402_db):
    """098 close-out MO-05: the owner reactivate is a CAS on status AND the
    paid_through it read — a settlement that landed first wins."""
    sid = await _sub(x402_db, int(time.time()) - 60 * 86400, status="suspended")
    real = subs.get_subscription

    async def stale(*a, **kw):
        row = await real(*a, **kw)
        # a settlement lands first: active, one period from now
        await x402_db.execute("UPDATE subscriptions SET status='active', paid_through=? "
                              "WHERE id=?", (int(time.time()) + 30 * 86400, sid))
        return row
    subs_get = subs.get_subscription
    subs.get_subscription = stale
    try:
        ok, detail = await subs.reactivate_subscription(sid, user_id="alice", db=x402_db)
    finally:
        subs.get_subscription = subs_get
    assert not ok and "changed meanwhile" in detail
