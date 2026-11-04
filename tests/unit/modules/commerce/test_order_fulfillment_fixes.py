"""099 review fixes: a fulfiller that never writes its goal runs out of attempts (no
endless retry and notice), and a marketplace row is ordered at most once."""
import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import service
from tests.unit.modules.commerce.test_order_review_fixes import IN, _live, notices  # noqa: F401
from tests.unit.modules.commerce.test_order_service_e2e import _env  # noqa: F401


def _paid_order():
    o = ords.create(offer_id="custom-art", offer_version=1, user_id="owner", channel="a2a",
                    buyer="b", inputs=IN, amount_usd="5.00", spend_budget=0.5)
    ords.attach_invoice(o.id, "inv_1", "5.00")
    return ords.mark_paid("inv_1", now=0)


@pytest.mark.asyncio
async def test_a_fulfiller_that_keeps_failing_ends_in_failed_with_notices(notices):
    _live()          # max_attempts defaults to 2

    def _boom(order, m, token):
        raise RuntimeError("goal board is full")
    hooks.register_fulfiller(_boom)
    order = _paid_order()
    t = 1000.0
    tok = ords.claim_fulfillment(order.id, now=t)
    assert tok and ords.get(order.id).attempts == 1
    t += ords.CLAIM_STALE_S + 1
    out = await service.tick(now=t)                 # reclaim = attempt 2, fails again
    assert out["reclaimed"] == 0 and ords.get(order.id).attempts == 2
    ords_row = ords.get(order.id)
    assert ords_row.status == ords.FULFILLING
    t += ords.CLAIM_STALE_S + 1
    out = await service.tick(now=t)                 # out of attempts -> failed
    assert out["failed"] == 1 and ords.get(order.id).status == ords.FAILED
    assert any(who == "owner" and "FAILED" in x and "refund" in x for who, x in notices)
    assert any(who == "buyer" and "could not be fulfilled" in x for who, x in notices)
    n = len(notices)
    await service.tick(now=t + 10 * ords.CLAIM_STALE_S)
    assert len(notices) == n                         # nothing loops any more


class _Market:
    kind = "market:acme"

    def __init__(self, rows):
        self.rows, self.replies = rows, []

    def publish(self, m, *, offer_url):
        return None

    def withdraw(self, m):
        return None

    def sync_orders(self):
        return list(self.rows)

    def reply(self, ref, payload):
        self.replies.append((ref, payload))


@pytest.mark.asyncio
async def test_market_rows_without_ref_are_skipped_and_refusals_answered_once(monkeypatch):
    service._REFUSED.clear()
    _live()                                          # not listed on market:acme
    m = _Market([{"offer_id": "custom-art", "inputs": IN, "buyer": "x"},
                 {"offer_id": "custom-art", "ref": "r-1", "inputs": IN}])
    hooks.register_listing_adapter(m)
    try:
        assert await service._sync_markets(None) == 0
        assert await service._sync_markets(None) == 0
        assert ords.list_orders() == []              # the ref-less row made no order
        assert [r for r, _ in m.replies] == ["r-1"]  # the refusal answered ONCE
        assert "not sold here" in m.replies[0][1]["error"]
    finally:
        hooks.unregister_listing_adapter("market:acme")
        service._REFUSED.clear()
