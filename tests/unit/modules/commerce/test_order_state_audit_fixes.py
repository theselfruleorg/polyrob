"""Order state-machine audit fixes (2026-11-10): an order that did not fail now sends no
failure notice, a refund is a recorded obligation, open orders expire, and a held
thread notice is never cleared by an older send."""
import pytest

from core.commerce import orders as ords
from modules.commerce import service
from tests.unit.modules.commerce.test_order_service_e2e import _env  # noqa: F401  (autouse)
from tests.unit.modules.commerce.test_order_stops_its_goal import _fulfilling, stopped  # noqa: F401


@pytest.fixture
def notices(monkeypatch):
    out = []

    async def _owner(container, user_id, text):
        out.append(("owner", text))

    async def _buyer(container, order, text, **k):
        out.append(("buyer", text))
        return False
    monkeypatch.setattr(service, "owner_notice", _owner)
    monkeypatch.setattr(service, "tell_buyer", _buyer)
    return out


@pytest.mark.asyncio
async def test_failing_a_delivered_order_sends_no_failure_notice(stopped, notices):
    order = _fulfilling(max_attempts=1)
    ords.deliver(order.id, {"tx_hash": "0x" + "ef" * 32}, accept_now=True)
    cur = await service.fail(order.id, "changed my mind", user_id="owner")
    assert cur.status == ords.ACCEPTED
    assert notices == []          # nobody is told "refund" for a delivered order
    assert stopped == []          # and no goal is stopped


# -- a refund is a recorded obligation, not income -----------------------------------

ADDR = "0x" + "ab" * 20
IN = {"delivery_address": ADDR, "brief": "a cat"}


async def _paid_order(tmp_path):
    from core.commerce import store as st
    from modules.x402 import invoicing
    from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    o = st.save_draft("owner", MANIFEST)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    order, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN, db=db)
    assert await invoicing.settle_payment_request(order.request_id,
                                                  transaction_hash="0x" + "cd" * 32, db=db)
    return db, order


async def _invoice_status(db, rid):
    row = await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id = ?", (rid,))
    return row["status"]


@pytest.mark.asyncio
async def test_owner_refund_moves_the_invoice_to_refund_due_and_lists_it(tmp_path, notices):
    from modules.commerce import owner_verb
    from modules.x402 import invoicing
    db, order = await _paid_order(tmp_path)
    try:
        await service.on_settled({"request_id": order.request_id, "user_id": "owner",
                                  "transaction_hash": "0x" + "cd" * 32}, db=db)
        assert ords.get(order.id).status == ords.PAID
        text = await owner_verb.offer_reply("owner", ["resolve", order.id, "refund"], db=db)
        assert "FAILED" in text and f"/invoices refunded {order.request_id}" in text
        assert await _invoice_status(db, order.request_id) == "refund_due"
        rows = await invoicing.list_payment_requests(user_id="owner", status="refund_due", db=db)
        assert [r["request_id"] for r in rows] == [order.request_id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_payment_for_a_cancelled_order_is_owed_back(tmp_path, notices):
    db, order = await _paid_order(tmp_path)
    try:
        ords.cancel(order.id)
        await service.on_settled({"request_id": order.request_id, "user_id": "owner"}, db=db)
        assert await _invoice_status(db, order.request_id) == "refund_due"
        assert any(who == "owner" and "/invoices refund_due" in t for who, t in notices)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_failed_fulfillment_marks_its_invoice_refund_due(tmp_path, notices):
    db, order = await _paid_order(tmp_path)
    try:
        await service.on_settled({"request_id": order.request_id, "user_id": "owner"}, db=db)
        for _ in range(6):                      # every attempt passes its deadline
            token = ords.claim_fulfillment(order.id, now=1000.0)
            assert ords.set_goal(order.id, token, "g_1")
            await service.tick(now=1000.0 + 10 ** 7, db=db)
            if ords.get(order.id).status == ords.FAILED:
                break
        assert ords.get(order.id).status == ords.FAILED
        assert await _invoice_status(db, order.request_id) == "refund_due"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_refund_of_a_fulfilling_order_stops_its_goal(stopped, notices):
    from modules.commerce import owner_verb
    order = _fulfilling(max_attempts=3)
    text = await owner_verb.offer_reply("owner", ["resolve", order.id, "refund"])
    assert "FAILED" in text
    assert stopped == ["g_old"]


@pytest.mark.asyncio
async def test_an_order_invoice_is_never_settled_by_attestation_alone(tmp_path):
    from core.commerce import store as st
    from modules.x402 import invoicing
    from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", MANIFEST)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        order, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN, db=db)
        assert not await invoicing.settle_payment_request(order.request_id, db=db)
        assert await _invoice_status(db, order.request_id) == "pending"
        assert await invoicing.settle_payment_request(order.request_id,
                                                      transaction_hash="0x" + "cd" * 32, db=db)
    finally:
        await db.close()


# -- the one-slot thread-notice outbox -------------------------------------------------

class _Chan:
    surface = "x"

    def __init__(self, on_reply=None, ok=True):
        self.on_reply, self.ok, self.sent = on_reply, ok, []

    def can_reply(self):
        return True

    async def reply(self, post_id, text, *, subject="", user_id=""):
        self.sent.append(text)
        if self.on_reply:
            self.on_reply()
        return self.ok


@pytest.fixture
def chan_x():
    from core.commerce import hooks
    service._LAST_TRY.clear()
    holder = {}

    def _register(ch):
        hooks.register_thread_channel("x", ch)
        holder["ch"] = ch
        return ch
    yield _register
    hooks.unregister_thread_channel("x")
    service._LAST_TRY.clear()


def _thread_order(now):
    o = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="x", buyer="42",
                    inputs={}, amount_usd="1", spend_budget=0, thread_ref="101", now=now)
    ords.attach_invoice(o.id, "r_thread", "1", now=now)
    return ords.mark_paid("r_thread", now=now)        # paid: no expiry step touches it


@pytest.mark.asyncio
async def test_a_retried_old_notice_never_clears_a_newer_one(chan_x):
    o = _thread_order(1000.0)
    ords.set_notice_pending(o.id, "Payment received", now=1000.0)
    # while the old notice is being sent, a newer one is held (its own send failed)
    chan_x(_Chan(on_reply=lambda: ords.set_notice_pending(o.id, "Order delivered",
                                                         now=1001.0)))
    out = await service.tick(now=1100.0)
    assert out["notices"] == 1
    assert ords.get(o.id).notice_pending == "Order delivered"


@pytest.mark.asyncio
async def test_a_failed_retry_never_overwrites_a_newer_notice(chan_x):
    o = _thread_order(1000.0)
    ords.set_notice_pending(o.id, "Payment received", now=1000.0)
    chan_x(_Chan(ok=False, on_reply=lambda: ords.set_notice_pending(o.id, "Order delivered",
                                                                   now=1001.0)))
    await service.tick(now=1100.0)
    assert ords.get(o.id).notice_pending == "Order delivered"


@pytest.mark.asyncio
async def test_the_give_up_clock_starts_when_the_notice_is_held(chan_x, monkeypatch):
    told = []

    async def _owner(container, uid, text):
        told.append(text)
    monkeypatch.setattr(service, "owner_notice", _owner)
    o = _thread_order(1000.0)                       # the order moved long ago
    late = 1000.0 + 10 * 3600
    ords.set_notice_pending(o.id, "A payment arrived for a cancelled order", now=late)
    chan_x(_Chan(ok=False))
    await service.tick(now=late + service.NOTICE_RETRY_S + 1)   # first retry fails
    assert told == []                                # not given up after one retry
    assert ords.get(o.id).notice_pending
    await service.tick(now=late + service.NOTICE_GIVE_UP_S + service.NOTICE_RETRY_S * 2)
    assert len(told) == 1 and ords.get(o.id).notice_pending is None


@pytest.mark.asyncio
async def test_a_quoted_order_is_invoiced_in_the_rail_the_buyer_chose(tmp_path):
    from core.commerce import store as st
    from tests.unit.core.commerce.test_offer_store_orders import _m
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    try:
        m = _m(pricing={"kind": "quote"}, rails=[{"asset": "usdc-base", "mode": "transfer"},
                                                 {"asset": "usdg-robinhood", "mode": "transfer"}])
        o = st.save_draft("owner", m)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        order, pay = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN,
                                                asset="usdg-robinhood", db=db)
        assert pay is None and order.asset == "usdg-robinhood"
        _, view = await service.quote_order(order.id, "12", user_id="owner", db=db)
        assert view["asset"] == "usdg-robinhood"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof,refused", [("file_hash", True), ("url", False)])
async def test_a_file_order_is_taken_only_where_the_file_can_reach_the_buyer(tmp_path, proof,
                                                                           refused):
    from core.commerce import store as st
    from tests.unit.core.commerce.test_offer_store_orders import _m
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", _m(deliverable={"kind": "file", "proof": proof}))
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        if refused:
            with pytest.raises(service.OrderRefused, match="direct"):
                await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN, db=db)
            with pytest.raises(service.OrderRefused, match="direct"):
                await service.create_order(o.id, channel="telegram", buyer="c", inputs=IN,
                                           db=db, thread_ref="101")
        else:
            order, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN,
                                                  db=db)
            assert order.status == ords.AWAITING
        order, _ = await service.create_order(o.id, channel="telegram", buyer="d", inputs=IN,
                                              db=db)
        assert order.status == ords.AWAITING        # a direct chat always takes it
    finally:
        await db.close()


# -- an unpaid order no payment can reach expires ------------------------------------

@pytest.mark.asyncio
async def test_a_quote_never_priced_expires(tmp_path, notices):
    from core.commerce import store as st
    from tests.unit.core.commerce.test_offer_store_orders import _m
    o = st.save_draft("owner", _m(pricing={"kind": "quote"}))
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    order, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN, now=1000.0)
    out = await service.tick(now=1000.0 + service.QUOTE_TTL_S - 60)
    assert out["expired"] == 0 and ords.get(order.id).status == ords.QUOTED
    out = await service.tick(now=1000.0 + service.QUOTE_TTL_S + 60)
    assert out["expired"] == 1 and ords.get(order.id).status == ords.EXPIRED
    assert any(who == "buyer" and "never priced" in t for who, t in notices)


@pytest.mark.asyncio
async def test_an_awaiting_order_whose_invoice_was_closed_expires(tmp_path, notices):
    """A crash inside a cancel: the invoice is closed (its expiry notice marked
    delivered), the order still says awaiting_payment."""
    from core.commerce import store as st
    from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", MANIFEST)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        order, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN, db=db)
        stray = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="a2a",
                            buyer="c", inputs=IN, amount_usd="5", spend_budget=0)
        assert await service._close_invoice(order.request_id, db=db) is None
        late = order.created_at + service.INVOICE_HOURS * 3600 + service.UNPAID_GRACE_S + 5
        out = await service.tick(now=order.created_at + 60, db=db)
        assert out["expired"] == 0                      # inside its window: untouched
        out = await service.tick(now=late, db=db)
        assert out["expired"] == 2                      # the closed one and the one with none
        assert ords.get(order.id).status == ords.EXPIRED
        assert ords.get(stray.id).status == ords.EXPIRED
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_awaiting_order_with_a_pending_invoice_is_left_to_the_watcher(tmp_path,
                                                                                notices):
    from core.commerce import store as st
    from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", MANIFEST)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        order, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN, db=db)
        out = await service.tick(now=order.created_at + 10 * 3600, db=db)
        assert out["expired"] == 0 and ords.get(order.id).status == ords.AWAITING
    finally:
        await db.close()


# -- retiring an offer closes its unpaid orders -------------------------------------

@pytest.mark.asyncio
async def test_retire_cancels_unpaid_orders_and_keeps_paid_ones(tmp_path, notices):
    from core.commerce import store as st
    from modules.commerce import owner_verb
    from modules.x402 import invoicing
    from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
    from tests.unit.modules.commerce.test_order_service_e2e import _db
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", MANIFEST)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        unpaid, _ = await service.create_order(o.id, channel="a2a", buyer="a", inputs=IN, db=db)
        inflight, _ = await service.create_order(o.id, channel="a2a", buyer="b", inputs=IN,
                                                 db=db)
        paid, _ = await service.create_order(o.id, channel="a2a", buyer="c", inputs=IN, db=db)
        assert await invoicing.claim_for_settlement(inflight.request_id, db=db)
        assert await invoicing.settle_payment_request(paid.request_id,
                                                      transaction_hash="0x" + "cd" * 32, db=db)
        await service.on_settled({"request_id": paid.request_id, "user_id": "owner"}, db=db)

        text = await owner_verb.offer_reply("owner", ["retire", o.id], db=db)
        assert "RETIRED" in text and "1 unpaid order(s) cancelled" in text
        assert "1 unpaid order(s) could not be cancelled" in text      # payment in flight
        assert ords.get(unpaid.id).status == ords.CANCELLED
        assert await _invoice_status(db, unpaid.request_id) == "expired"
        assert ords.get(inflight.id).status == ords.AWAITING
        assert ords.get(paid.id).status == ords.PAID                    # still fulfilled
        assert any(who == "buyer" and "retired" in t for who, t in notices)

        # money that still completes for a retired-and-cancelled order is owed back
        await db.execute("UPDATE x402_payment_requests SET status='completed' WHERE id = ?",
                         (unpaid.request_id,))
        await service.on_settled({"request_id": unpaid.request_id, "user_id": "owner"}, db=db)
        assert ords.get(unpaid.id).status == ords.CANCELLED
        assert await _invoice_status(db, unpaid.request_id) == "refund_due"
    finally:
        await db.close()
