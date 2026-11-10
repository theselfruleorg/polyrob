"""The expiry step never starves: 50 old unpaid orders it can never expire (their
invoice stuck in `settling`) do not hide a later one for ever. The rows it looked at
and skipped go to the back of the line (`updated_at`)."""
import pytest

from core.commerce import orders as ords
from modules.commerce import service
from tests.unit.modules.commerce.test_order_service_e2e import _env  # noqa: F401  (autouse)


class _SettlingDb:
    async def fetch_one(self, sql, args):
        return {"status": "settling"}


@pytest.mark.asyncio
async def test_51_unpaid_orders_rotate_and_the_last_one_expires(monkeypatch):
    async def _resolve(db=None):
        return _SettlingDb()
    monkeypatch.setattr("modules.x402._db.resolve_db", _resolve)

    async def _buyer(container, order, text, **k):
        return False
    monkeypatch.setattr(service, "tell_buyer", _buyer)
    for i in range(50):
        o = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="a2a",
                        buyer=f"b{i}", inputs={}, amount_usd="5", spend_budget=0, now=1000.0 + i)
        ords.attach_invoice(o.id, f"r_{i}", "5", now=1000.0 + i)
    last = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="a2a",
                       buyer="late", inputs={}, amount_usd="5", spend_budget=0, now=2000.0)
    now = 2000.0 + 10 ** 6
    first = await service._expire_unpayable(None, None, now, None)
    second = await service._expire_unpayable(None, None, now + 60, None)
    assert first + second == 1
    assert ords.get(last.id).status == ords.EXPIRED
    assert all(o.status == ords.AWAITING
               for o in ords.list_orders(statuses=[ords.AWAITING], limit=100))
