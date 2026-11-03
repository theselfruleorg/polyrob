"""098 W1 MO-03: ``refund_due`` is a guarded transition with an exit.

The event and the owner notice follow only a real ``completed|settled_no_tx ->
refund_due`` move; ``refund_due -> refunded`` (terminal) is the owner's verb and
the `/status` refund-owed line stops counting the row.
"""
import pytest

from modules.x402 import x402_integration as xi


class _Container:
    def __init__(self, db):
        self._db = db

    def get_service(self, name):
        return self._db if name == "database_manager" else None


async def _seed(db, rid, status):
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES (?,'10000',0.01,'usdc','base','0xt',?,9999999999,?,"
        "'{\"kind\":\"machine_payment\"}')", (rid, rid, status))


@pytest.fixture
def notices(monkeypatch):
    told = []

    async def tell(container, pid):
        told.append(pid)
    monkeypatch.setattr(xi, "_tell_owner_refund_due", tell)
    monkeypatch.setattr(xi, "_emit_refund_due", lambda pid: None)
    return told


async def _status(db, rid):
    return (await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?",
                               (rid,)))["status"]


@pytest.mark.asyncio
async def test_refund_due_no_row_returns_false_and_no_notice(x402_db, notices):
    assert await xi.mark_payment_refund_due("nope", container=_Container(x402_db)) is False
    assert notices == []


@pytest.mark.asyncio
async def test_refund_due_twice_notifies_once(x402_db, notices):
    await _seed(x402_db, "p1", "completed")
    c = _Container(x402_db)
    assert await xi.mark_payment_refund_due("p1", container=c) is True
    assert await xi.mark_payment_refund_due("p1", container=c) is False
    assert notices == ["p1"]
    assert await _status(x402_db, "p1") == "refund_due"


@pytest.mark.asyncio
async def test_expired_row_cannot_become_refund_due(x402_db, notices):
    await _seed(x402_db, "p2", "expired")
    assert await xi.mark_payment_refund_due("p2", container=_Container(x402_db)) is False
    assert await _status(x402_db, "p2") == "expired"
    assert notices == []


@pytest.mark.asyncio
async def test_owner_refunded_closes_health_item(x402_db, notices):
    await _seed(x402_db, "p3", "settled_no_tx")
    assert await xi.mark_payment_refund_due("p3", container=_Container(x402_db))
    assert await xi.mark_payment_refunded("p3", tx_hash="0xrefund", note="sent back",
                                          db=x402_db) is True
    row = await x402_db.fetch_one("SELECT * FROM x402_payment_requests WHERE id='p3'")
    assert row["status"] == "refunded"
    assert row["refund_tx_hash"] == "0xrefund" and row["refunded_at"]
    assert await xi.mark_payment_refunded("p3", db=x402_db) is False  # terminal
    n = await x402_db.fetch_one(
        "SELECT COUNT(*) AS n FROM x402_payment_requests WHERE status='refund_due'")
    assert n["n"] == 0
