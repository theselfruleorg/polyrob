"""Money loop — agent-initiated payment requests (invoices).

Runs against the REAL x402 schema (the all-fakes pattern is why N1 shipped).
Contract: pending rows carry kind=agent_invoice + session provenance in
metadata; amounts are ceiling-bounded; creation is per-day capped; settlement is
an explicit pending→completed transition; expiry honors the deadline column.
"""
import json

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_create_persists_pending_invoice_row(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_1", amount_usd=5.0,
            purpose="research report", db=db,
        )
        assert inv["status"] == "pending"
        assert inv["recipient"] == "0xTREASURY"
        row = await db.fetch_one(
            "SELECT * FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
        assert row is not None and row["status"] == "pending"
        # DatabaseConnection.fetch_* auto-parses JSON-looking TEXT columns
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        assert meta["kind"] == "agent_invoice"
        assert meta["session_id"] == "sess_1"
        assert meta["tenant_id"] == "rob"
        assert meta["wake_delivered"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_payment_request_returns_row(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        created = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="test", db=db)
        got = await invoicing.get_payment_request(created["request_id"], db=db)
        assert got is not None
        assert got["request_id"] == created["request_id"]
        assert got["amount_usd"] == 5.0
        assert got["status"] == "pending"
        assert got["recipient"] and got["chain"] and got["nonce"]
        assert got["purpose"] == "test"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_payment_request_missing_returns_none(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        assert await invoicing.get_payment_request("inv_doesnotexist", db=db) is None
        assert await invoicing.get_payment_request("", db=db) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_stores_correspondent_ref(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z", "thread_id": ""})
        rows = await invoicing.settled_unnotified_invoices(db=db)  # none settled yet
        assert rows == []
        # settle then read the linkage through the watcher's read path
        await invoicing.settle_payment_request(inv["request_id"], transaction_hash="0xabc", db=db)
        settled = await invoicing.settled_unnotified_invoices(db=db)
        assert len(settled) == 1
        assert settled[0]["correspondent_ref"] == {
            "surface": "email", "address": "x@y.z", "thread_id": ""}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_drops_invalid_correspondent_ref(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=3.0, purpose="svc", db=db,
            correspondent_ref={"surface": "", "address": "x@y.z"})
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        settled = await invoicing.settled_unnotified_invoices(db=db)
        assert settled[0]["correspondent_ref"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_amount_ceiling_enforced(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_INVOICE_MAX_USD", "10")
        with pytest.raises(ValueError, match="ceiling"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=11.0, purpose="too big", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_daily_cap_enforced(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_INVOICE_DAILY_MAX", "2")
        for i in range(2):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=1.0, purpose=f"job {i}", db=db)
        with pytest.raises(ValueError, match="daily"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=1.0, purpose="job 3", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_treasury_refused(tmp_path, monkeypatch):
    db = await _setup_db(tmp_path)
    try:
        monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "")
        with pytest.raises(ValueError, match="treasury"):
            await invoicing.create_payment_request(
                user_id="rob", session_id="s", amount_usd=1.0, purpose="p", db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settle_transitions_pending_once(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=2.0, purpose="p", db=db)
        rid = inv["request_id"]
        assert await invoicing.settle_payment_request(rid, transaction_hash="0xabc", db=db) is True
        # idempotent: second settle is a no-op
        assert await invoicing.settle_payment_request(rid, db=db) is False
        row = await db.fetch_one("SELECT * FROM x402_payment_requests WHERE id=?", (rid,))
        assert row["status"] == "completed" and row["transaction_hash"] == "0xabc"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expiry_marks_stale_pending(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=2.0, purpose="p",
            expiry_hours=1.0, db=db)
        # nothing expires yet
        assert await invoicing.expire_stale_requests(db=db) == []
        # jump past the deadline
        expired = await invoicing.expire_stale_requests(
            db=db, now=inv["expires_at_epoch"] + 10)
        assert [e["request_id"] for e in expired] == [inv["request_id"]]
        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id=?", (inv["request_id"],))
        assert row["status"] == "expired"
        # an expired invoice can no longer be settled
        assert await invoicing.settle_payment_request(inv["request_id"], db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settled_unnotified_and_mark_delivered(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_9", amount_usd=3.0, purpose="p", db=db)
        assert await invoicing.settled_unnotified_invoices(db=db) == []
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        pending_wakes = await invoicing.settled_unnotified_invoices(db=db)
        assert len(pending_wakes) == 1
        assert pending_wakes[0]["session_id"] == "sess_9"
        assert pending_wakes[0]["user_id"] == "rob"
        await invoicing.mark_wake_delivered(inv["request_id"], db=db)
        assert await invoicing.settled_unnotified_invoices(db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_list_is_tenant_scoped(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=1.0, purpose="mine", db=db)
        await invoicing.create_payment_request(
            user_id="other", session_id="s2", amount_usd=1.0, purpose="theirs", db=db)
        mine = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert len(mine) == 1 and mine[0]["purpose"] == "mine"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_tenant_refused_everywhere(tmp_path):
    # An empty user_id would create a SHARED anonymous bucket (cross-tenant
    # reads + shared daily cap) — creation raises, listing returns nothing.
    db = await _setup_db(tmp_path)
    try:
        with pytest.raises(ValueError, match="authenticated tenant"):
            await invoicing.create_payment_request(
                user_id="", session_id="s", amount_usd=1.0, purpose="p", db=db)
        assert await invoicing.list_payment_requests(user_id="", db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_wake_is_atomic_single_winner(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=1.0, purpose="p", db=db)
        await invoicing.settle_payment_request(inv["request_id"], db=db)
        assert await invoicing.claim_wake(inv["request_id"], db=db) is True
        # a racing second claimer loses
        assert await invoicing.claim_wake(inv["request_id"], db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_for_settlement_is_exclusive(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        rid = inv["request_id"]
        # first claim wins, second loses (already settling)
        assert await invoicing.claim_for_settlement(rid, db=db) is True
        assert await invoicing.claim_for_settlement(rid, db=db) is False
        # settle works from 'settling'
        assert await invoicing.settle_payment_request(rid, transaction_hash="0xt", db=db) is True
        # now settled -> claim fails
        assert await invoicing.claim_for_settlement(rid, db=db) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_revert_settlement_claim_restores_pending(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        rid = inv["request_id"]
        assert await invoicing.claim_for_settlement(rid, db=db) is True
        await invoicing.revert_settlement_claim(rid, db=db)
        got = await invoicing.get_payment_request(rid, db=db)
        assert got["status"] == "pending"  # payable again
        # a completed row is never resurrected by revert
        await invoicing.claim_for_settlement(rid, db=db)
        await invoicing.settle_payment_request(rid, db=db)
        await invoicing.revert_settlement_claim(rid, db=db)
        got = await invoicing.get_payment_request(rid, db=db)
        assert got["status"] == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_direct_settle_still_works_from_pending(tmp_path):
    # polyrob owner settle calls settle_payment_request directly on a 'pending' row.
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p", db=db)
        assert await invoicing.settle_payment_request(inv["request_id"], db=db) is True
    finally:
        await db.close()
