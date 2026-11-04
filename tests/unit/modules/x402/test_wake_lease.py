"""098 W1 MO-04: the settlement/expiry wake is a lease. A notice that raises
leaves the claim; the reaper re-opens it; three failures reach the owner ONCE;
a retried room-action wake applies once and never reports a "late" payment;
an expired room-action invoice gets its notice."""
import json
import time
import uuid

import pytest

from modules.x402 import invoicing, wake_lease
from modules.x402.settlement_notify import SettlementNotifyMixin


async def _mint(db, *, status="completed", kind="agent_invoice", extra=None):
    rid = f"inv_{uuid.uuid4().hex[:10]}"
    meta = {"kind": kind, "tenant_id": "u1", "wake_delivered": False, "session_id": "s1",
            **(extra or {})}
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES (?,'1',1.0,'usdc','base','0xt',?,1,?,?)",
        (rid, rid, status, json.dumps(meta)))
    return rid


async def _meta(db, rid):
    row = await db.fetch_one("SELECT metadata FROM x402_payment_requests WHERE id=?", (rid,))
    m = row["metadata"]
    return m if isinstance(m, dict) else json.loads(m)


class _W(SettlementNotifyMixin):
    def __init__(self, db):
        self._db = db
        self.notices = []

    async def _push_owner_notice(self, user_id, text, **kw):
        self.notices.append(text)


@pytest.mark.asyncio
async def test_notify_exception_leaves_claim_and_reaper_retries(x402_db):
    rid = await _mint(x402_db)
    w = _W(x402_db)

    async def boom(inv):
        raise RuntimeError("container missing")
    with pytest.raises(RuntimeError):
        await w._wake_once({"request_id": rid}, boom)
    assert (await _meta(x402_db, rid))["wake_state"] == "claimed"
    assert [i["request_id"] for i in await invoicing.settled_unnotified_invoices(db=x402_db)] == []
    assert await wake_lease.reap_stale_wake_claims(db=x402_db, now=time.time() + 901) == []
    assert rid in [i["request_id"] for i in await invoicing.settled_unnotified_invoices(db=x402_db)]
    seen = []

    async def ok(inv):
        seen.append(inv["request_id"])
    assert await w._wake_once({"request_id": rid}, ok)
    meta = await _meta(x402_db, rid)
    assert seen == [rid] and meta["wake_state"] == "delivered" and meta["wake_attempts"] == 1


@pytest.mark.asyncio
async def test_three_failed_attempts_escalate_to_owner(x402_db):
    rid = await _mint(x402_db)
    w = _W(x402_db)

    async def boom(inv):
        raise RuntimeError("store busy")
    t = time.time()
    for n in range(wake_lease.WAKE_MAX_ATTEMPTS):
        with pytest.raises(RuntimeError):
            await w._wake_once({"request_id": rid}, boom)
        t += 1000
        out = await wake_lease.reap_stale_wake_claims(db=x402_db, now=t + 2000 * n)
    assert [o["request_id"] for o in out] == [rid]
    meta = await _meta(x402_db, rid)
    assert meta["wake_state"] == "delivered" and meta["wake_delivered"] in (True, 1)
    assert await wake_lease.reap_stale_wake_claims(db=x402_db, now=t + 10 ** 6) == []


@pytest.mark.asyncio
async def test_room_action_retry_applies_once_not_late(monkeypatch):
    applied, late = [], []
    state = {"status": "paid"}  # the first attempt paid it, then crashed

    class _Row:
        invoice_id = "inv_1"

        @property
        def status(self):
            return state["status"]
    monkeypatch.setattr("core.surfaces.room_actions.mark_settled", lambda c, oid, **k: False)
    monkeypatch.setattr("core.surfaces.room_actions._store",
                        lambda c: type("S", (), {"get": lambda s, o: _Row()})())

    async def fake_apply(container, offer_id, **kw):
        applied.append(offer_id)
        state["status"] = "applied"
        return type("R", (), {"ok": True, "text": "done"})()
    monkeypatch.setattr("core.surfaces.room_actions.apply", fake_apply)

    class _RW(SettlementNotifyMixin):
        _db = None
        _room_moderator = None
        _room_container = object()

        async def _report_late_room_payment(self, inv, offer_id):
            late.append(offer_id)

        async def _deliver_room_action_result(self, inv, offer_id, result):
            pass
    inv = {"request_id": "inv_1", "kind": "room_action", "room_action": {"offer_id": "off_1"},
           "amount_usd": 0.5, "user_id": "u", "session_id": ""}
    w = _RW()
    await w._apply_room_action(dict(inv))   # retry after the crash: applies
    await w._apply_room_action(dict(inv))   # a second retry: already applied
    assert applied == ["off_1"] and late == []


@pytest.mark.asyncio
async def test_expired_room_action_invoice_is_notified(x402_db):
    rid = await _mint(x402_db, status="expired", kind="room_action",
                      extra={"room_action": {"offer_id": "off_9"}})
    rows = await invoicing.expired_unnotified_invoices(db=x402_db)
    assert [(r["request_id"], r["kind"]) for r in rows] == [(rid, "room_action")]
    posted = []

    class _EW(_W):
        async def _deliver_session_notice(self, inv, text, kind):
            return False

        async def _post_to_room(self, offer_id, text):
            posted.append(offer_id)
            return True
    w = _EW(x402_db)
    assert await w._wake_once(rows[0], w._notify_expired)
    assert posted == ["off_9"] and w.notices
    assert (await _meta(x402_db, rid))["wake_state"] == "delivered"


def _offer_store_with(monkeypatch, status):
    row = type("Row", (), {"status": status, "invoice_id": ""})()
    monkeypatch.setattr("core.surfaces.room_actions._store",
                        lambda c: type("S", (), {"get": lambda s, o: row})())


@pytest.mark.asyncio
@pytest.mark.parametrize("offer_status,word", [("refused", "withdrawn"),
                                               ("expired", "expired unpaid")])
async def test_expired_invoice_notice_names_what_happened(x402_db, monkeypatch,
                                                          offer_status, word):
    """No silent expiry: a cancelled (refused) offer's invoice still reaches
    the room, the owner and the session — with words that say it was
    withdrawn. An offer that timed out says "expired unpaid"."""
    rid = await _mint(x402_db, status="expired", kind="room_action",
                      extra={"room_action": {"offer_id": "off_c"}})
    rows = await invoicing.expired_unnotified_invoices(db=x402_db)
    _offer_store_with(monkeypatch, offer_status)
    posted, sessions = [], []

    class _EW(_W):
        _room_container = object()

        async def _deliver_session_notice(self, inv, text, kind):
            sessions.append(text)
            return False

        async def _post_to_room(self, offer_id, text):
            posted.append(text)
            return True
    w = _EW(x402_db)
    assert await w._wake_once(rows[0], w._notify_expired)
    assert len(posted) == 1 and word in posted[0]
    assert len(sessions) == 1 and w.notices
    if offer_status == "refused":
        assert "unpaid" not in posted[0] and "cancelled or refused" in sessions[0]
    assert (await _meta(x402_db, rid))["wake_state"] == "delivered"


@pytest.mark.asyncio
async def test_unreadable_offer_store_still_notifies(x402_db, monkeypatch):
    rid = await _mint(x402_db, status="expired", kind="room_action",
                      extra={"room_action": {"offer_id": "off_x"}})
    rows = await invoicing.expired_unnotified_invoices(db=x402_db)

    def _boom(c):
        raise RuntimeError("store down")
    monkeypatch.setattr("core.surfaces.room_actions._store", _boom)
    posted = []

    class _EW(_W):
        _room_container = object()

        async def _deliver_session_notice(self, inv, text, kind):
            return False

        async def _post_to_room(self, offer_id, text):
            posted.append(offer_id)
            return True
    w = _EW(x402_db)
    assert await w._wake_once(rows[0], w._notify_expired)
    assert posted == ["off_x"] and w.notices
    assert rid
