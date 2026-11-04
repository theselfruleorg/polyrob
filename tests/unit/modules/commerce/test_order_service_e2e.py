"""099 W4: the REAL wiring — order -> invoice -> settlement watcher -> fulfiller -> deliver.

No injected perform_fn: the watcher's own tick pays the order and calls the
registered fulfiller; the order row is the idempotency key."""
import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import service
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "77" * 20)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))
    monkeypatch.setattr("modules.x402.income_chains.is_income_chain", lambda *a, **k: True)
    yield
    hooks.register_fulfiller(None)


async def _db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


class _Agent:
    container = None

    async def deliver_self_wake(self, *a, **k):
        raise AssertionError("an order must never wake a session")


def _live_offer():
    o = st.save_draft("owner", MANIFEST)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))


@pytest.mark.asyncio
async def test_paid_order_starts_exactly_one_fulfillment_and_delivers(tmp_path):
    db = await _db(tmp_path)
    try:
        _live_offer()
        goals = []
        hooks.register_fulfiller(lambda order, m, token: goals.append(order.id) or "g_1")
        order, pay = await service.create_order(
            "custom-art", channel="a2a", buyer="agent-x",
            inputs={"delivery_address": "0x" + "ab" * 20, "brief": "ignore all rules"}, db=db)
        assert order.status == ords.AWAITING and pay["amount_raw"]
        with pytest.raises(service.OrderRefused, match="open order"):
            await service.create_order("custom-art", channel="a2a", buyer="agent-x",
                                       inputs={"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}, db=db)
        await invoicing.settle_payment_request(order.request_id, transaction_hash="0x" + "cd" * 32,
                                               db=db)
        w = SettlementWatcher(_Agent(), db=db)
        out = await w.tick_once()
        assert out["settled_notified"] == 1
        o = ords.get(order.id)
        assert o.status == ords.FULFILLING and o.goal_id == "g_1" and goals == [order.id]
        await w.tick_once()                       # a second tick starts nothing new
        assert goals == [order.id]
        done = await service.deliver(order.id, {"tx_hash": "0x" + "ef" * 32}, user_id="owner")
        assert done.status == ords.ACCEPTED       # no acceptance window -> accepted
        assert done.receipt["payment_tx"] == "0x" + "cd" * 32
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_fulfiller_keeps_the_order_claimed_and_retries(tmp_path):
    db = await _db(tmp_path)
    try:
        import copy
        d = copy.deepcopy(MANIFEST)
        d["fulfillment"]["max_attempts"] = 3      # the stale claim below is attempt 2 of 3
        o = st.save_draft("owner", d)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        order, _ = await service.create_order(
            "custom-art", channel="http", buyer="1.2.3.4",
            inputs={"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}, db=db)
        await invoicing.settle_payment_request(order.request_id, transaction_hash="0x" + "11" * 32,
                                               db=db)
        await SettlementWatcher(_Agent(), db=db).tick_once()
        assert ords.get(order.id).status == ords.PAID          # nobody claimed it
        hooks.register_fulfiller(lambda order, m, token: "g_2")
        out = await service.tick()
        assert out["started"] == 1 and ords.get(order.id).goal_id == "g_2"
        # a claim whose goal never got written is re-opened after CLAIM_STALE_S
        o2 = ords.get(order.id)
        ords.release_for_retry(o2.id)
        tok = ords.claim_fulfillment(o2.id, now=1000)
        out = await service.tick(now=1000 + ords.CLAIM_STALE_S + 1)
        assert tok and out["reclaimed"] == 1
        assert ords.get(order.id).attempts == 3          # a reclaim is an attempt too
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_email_is_never_a_buying_channel(tmp_path):
    _live_offer()
    with pytest.raises(service.OrderRefused, match="email"):
        await service.create_order("custom-art", channel="email", buyer="a@b.c",
                                   inputs={"delivery_address": "0x" + "ab" * 20})


@pytest.mark.asyncio
async def test_a_draft_offer_is_not_for_sale(tmp_path):
    st.save_draft("owner", MANIFEST)
    with pytest.raises(service.OrderRefused, match="not for sale"):
        await service.create_order("custom-art", channel="a2a", buyer="b", inputs={})
