"""F11: a `/pay` completion moves its order at once, with or without a running
settlement watcher (`API_AUTONOMY_RUNTIME` off runs none in the api process).

`SettlementWatcher.settle_order_now` is the watcher's own claim-then-notify path
for ONE invoice: the order is paid and fulfillment starts, and a later tick (here
or in another process) finds the wake claimed and does nothing twice.
"""
import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from modules.commerce import service
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher
from tests.unit.modules.commerce.test_order_service_e2e import (  # noqa: F401 (fixture)
    _Agent, _db, _env, _live_offer)


@pytest.mark.asyncio
async def test_a_completed_order_invoice_moves_its_order_now(tmp_path):
    db = await _db(tmp_path)
    try:
        _live_offer()
        goals = []
        hooks.register_fulfiller(lambda order, m, token: goals.append(order.id) or "g_1")
        order, _ = await service.create_order(
            "custom-art", channel="http", buyer="1.2.3.4",
            inputs={"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}, db=db)
        await invoicing.settle_payment_request(order.request_id,
                                               transaction_hash="0x" + "cd" * 32, db=db)
        w = SettlementWatcher(_Agent(), db=db)
        assert await w.settle_order_now(order.request_id) is True
        assert ords.get(order.id).status == ords.FULFILLING and goals == [order.id]
        # exactly once: a second call and a later tick find the wake claimed
        assert await w.settle_order_now(order.request_id) is False
        assert (await w.tick_once())["settled_notified"] == 0
        assert goals == [order.id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_pending_or_unknown_invoice_moves_nothing(tmp_path):
    db = await _db(tmp_path)
    try:
        _live_offer()
        order, _ = await service.create_order(
            "custom-art", channel="http", buyer="1.2.3.4",
            inputs={"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}, db=db)
        w = SettlementWatcher(_Agent(), db=db)
        assert await w.settle_order_now(order.request_id) is False   # still pending
        assert await w.settle_order_now("inv_missing") is False
        assert ords.get(order.id).status == ords.AWAITING
    finally:
        await db.close()
