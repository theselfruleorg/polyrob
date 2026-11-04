"""099 review fixes: a cancelled order's invoice is closed, a payment for a closed
order is reported for a refund, a refused invoice never strands an order, and the
owner's quote reply says whether the buyer was told."""
import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import owner_verb, service
from modules.commerce import storefront as sf
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST, _m
from tests.unit.modules.commerce.test_order_service_e2e import _Agent, _db, _env  # noqa: F401

ADDR = "0x" + "ab" * 20
IN = {"delivery_address": ADDR, "brief": "a cat"}


@pytest.fixture(autouse=True)
def _reset_rate():
    sf._RATE.clear()
    yield
    sf._RATE.clear()


def _live(manifest=MANIFEST):
    o = st.save_draft("owner", manifest)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    return o


async def _status(db, rid):
    row = await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id = ?", (rid,))
    return row["status"]


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
async def test_buyer_cancel_closes_the_invoice_so_it_cannot_be_paid(tmp_path):
    db = await _db(tmp_path)
    try:
        _live()
        await sf.storefront_reply(None, "telegram", "42", f"/buy custom-art delivery_address={ADDR} brief=a cat",
                                  db=db)
        o = ords.list_orders(buyer="42")[0]
        text = await sf.storefront_reply(None, "telegram", "42", f"/order {o.id} cancel", db=db)
        assert "cancelled" in text and "do not pay" in text
        assert ords.get(o.id).status == ords.CANCELLED
        assert await _status(db, o.request_id) == "expired"
        # a late payment can no longer complete the invoice
        assert not await invoicing.settle_payment_request(
            o.request_id, transaction_hash="0x" + "cd" * 32, db=db)
        # and no "expired unpaid" notice follows the cancel
        assert await invoicing.expired_unnotified_invoices(db=db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_is_refused_while_a_payment_is_in_flight(tmp_path):
    db = await _db(tmp_path)
    try:
        _live()
        order, _ = await service.create_order("custom-art", channel="a2a", buyer="b",
                                              inputs=IN, db=db)
        assert await invoicing.claim_for_settlement(order.request_id, db=db)
        ok, text = await service.cancel_order(order, db=db)
        assert not ok and "settling" in text
        assert ords.get(order.id).status == ords.AWAITING
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_payment_for_a_cancelled_order_is_a_refund_not_queued_work(tmp_path, notices):
    db = await _db(tmp_path)
    try:
        _live()
        order, _ = await service.create_order("custom-art", channel="a2a", buyer="b",
                                              inputs=IN, db=db)
        ords.cancel(order.id)        # the order closed while its invoice stayed payable
        await invoicing.settle_payment_request(order.request_id,
                                               transaction_hash="0x" + "ee" * 32, db=db)
        w = SettlementWatcher(_Agent(), db=db)
        pushed = []

        async def _push(user_id, text, **k):
            pushed.append(text)
        w._push_owner_notice = _push
        await w.tick_once()
        assert ords.get(order.id).status == ords.CANCELLED
        owner = [t for who, t in notices if who == "owner"]
        assert owner and "CANCELLED" in owner[0] and "refund" in owner[0]
        assert any(who == "buyer" and "refund" in t for who, t in notices)
        assert not any("queued" in t or "PAID:" in t for t in pushed)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_paid_order_notice_names_its_real_state(tmp_path, notices):
    db = await _db(tmp_path)
    try:
        _live()
        order, _ = await service.create_order("custom-art", channel="a2a", buyer="b",
                                              inputs=IN, db=db)
        await invoicing.settle_payment_request(order.request_id,
                                               transaction_hash="0x" + "ef" * 32, db=db)
        w = SettlementWatcher(_Agent(), db=db)
        pushed = []

        async def _push(user_id, text, **k):
            pushed.append(text)
        w._push_owner_notice = _push
        await w.tick_once()
        assert ords.get(order.id).status == ords.PAID          # no fulfiller here
        assert any(f"Order {order.id} PAID" in t and "queued" in t for t in pushed)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_any_invoice_failure_cancels_the_half_made_order(tmp_path, monkeypatch):
    _live()

    async def _boom(**k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(invoicing, "create_payment_request", _boom)
    with pytest.raises(service.OrderRefused, match="could not be made"):
        await service.create_order("custom-art", channel="a2a", buyer="b",
                                   inputs=IN)
    assert [o.status for o in ords.list_orders(buyer="b")] == [ords.CANCELLED]


@pytest.mark.asyncio
async def test_a_refused_quote_keeps_the_order_quoted(tmp_path, monkeypatch):
    _live(_m(pricing={"kind": "quote"}))
    order, pay = await service.create_order("custom-art", channel="a2a", buyer="b",
                                            inputs=IN)
    assert pay is None and order.status == ords.QUOTED

    async def _refuse(**k):
        raise ValueError("over the invoice cap")
    monkeypatch.setattr(invoicing, "create_payment_request", _refuse)
    text = await owner_verb.offer_reply("owner", ["quote", order.id, "12"])
    assert text.startswith("❌") and "over the invoice cap" in text
    assert ords.get(order.id).status == ords.QUOTED


@pytest.mark.asyncio
async def test_quote_reply_says_when_the_buyer_was_not_told(tmp_path):
    db = await _db(tmp_path)
    try:
        _live(_m(pricing={"kind": "quote"}))
        order, _ = await service.create_order("custom-art", channel="telegram", buyer="42",
                                              inputs=IN)
        text = await owner_verb.offer_reply("owner", ["quote", order.id, "12"], db=db)
        assert "NOT sent" in text and f"/order {order.id}" in text
        assert "was sent the request" not in text
    finally:
        await db.close()


# -- caps: supply and per_day count paid orders (unpaid ones hold nothing, so
# anonymous unpaid orders cannot show "sold out"); a late payer over a cap is
# refunded, never fulfilled ---------------------------------------------------------

@pytest.mark.asyncio
async def test_unpaid_orders_do_not_sell_out_the_supply(tmp_path):
    db = await _db(tmp_path)
    try:
        _live(_m(caps={"supply": 1, "open_per_buyer": 5}))
        for b in ("t1", "t2", "t3"):   # anonymous, never paid
            await service.create_order("custom-art", channel="http", buyer=b, inputs=IN, db=db)
        assert ords.sold("custom-art") == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_paid_order_sells_out_the_supply(tmp_path):
    db = await _db(tmp_path)
    try:
        _live(_m(caps={"supply": 1}))
        a, _ = await service.create_order("custom-art", channel="a2a", buyer="b1", inputs=IN,
                                          db=db)
        await invoicing.settle_payment_request(a.request_id, transaction_hash="0x" + "a2" * 32,
                                               db=db)
        await service.on_settled({"request_id": a.request_id, "status": "completed",
                                  "user_id": "owner"})
        with pytest.raises(service.OrderRefused, match="sold out"):
            await service.create_order("custom-art", channel="a2a", buyer="b2", inputs=IN,
                                       db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_per_day_counts_only_paid_orders(tmp_path):
    db = await _db(tmp_path)
    try:
        _live(_m(caps={"per_day": 1, "open_per_buyer": 5}))
        # unpaid orders never fill the day
        for b in ("t1", "t2", "t3"):
            await service.create_order("custom-art", channel="http", buyer=b, inputs=IN, db=db)
        assert ords.paid_since("custom-art", 0) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_payment_over_the_supply_is_refunded_not_fulfilled(tmp_path, notices):
    from core.commerce import hooks
    db = await _db(tmp_path)
    try:
        _live(_m(caps={"supply": 1, "open_per_buyer": 5}))
        a, _ = await service.create_order("custom-art", channel="a2a", buyer="b1", inputs=IN,
                                          db=db)
        # a second order that slipped past the order-time check (a race, a quote)
        b = ords.create(offer_id="custom-art", offer_version=1, user_id="owner",
                        channel="a2a", buyer="b2", inputs=IN, amount_usd="5.00",
                        spend_budget=0.5)
        ords.attach_invoice(b.id, "inv_late", "5.00")
        goals = []
        hooks.register_fulfiller(lambda order, m, token: goals.append(order.id) or "g")
        await invoicing.settle_payment_request(a.request_id, transaction_hash="0x" + "a1" * 32,
                                               db=db)
        first = await service.on_settled({"request_id": a.request_id, "status": "completed",
                                          "user_id": "owner"})
        second = await service.on_settled({"request_id": "inv_late", "status": "completed",
                                           "user_id": "owner"})
        assert first.status == ords.FULFILLING and goals == [a.id]
        assert second.status == ords.FAILED and "sold out" in second.error
        owner = [t for who, t in notices if who == "owner"]
        assert any("FAILED" in t and "refund" in t and b.id in t for t in owner)
    finally:
        await db.close()


def test_the_setup_card_does_not_claim_the_budget_is_enforced():
    """Review C9: nothing enforces the setup budget, so no card says it does."""
    o = st.save_draft("owner", MANIFEST)
    body = owner_verb.setup_card_body(o, 3)
    assert "not a wallet cap" in body and "accounts) within $3" not in body
