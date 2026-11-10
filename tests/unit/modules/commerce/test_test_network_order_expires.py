"""An order paid on a TEST network never pays (the watcher returns before the order),
so it does not stay `awaiting_payment` for ever: the tick expires it and tells the buyer
that a test-network payment does not buy. A completed invoice on an income chain is
still the watcher's."""
import pytest

from core.commerce import orders as ords
from modules.commerce import service
from tests.unit.modules.commerce.test_order_service_e2e import _env  # noqa: F401  (autouse)


class _Db:
    def __init__(self, chain):
        self.chain = chain

    async def fetch_one(self, sql, args):
        return {"status": "completed", "chain": self.chain, "asset_id": f"usdc-{self.chain}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("chain,expired", [("base-sepolia", True), ("base", False)])
async def test_a_test_network_payment_expires_the_order(monkeypatch, chain, expired):
    monkeypatch.setattr("modules.x402.income_chains.is_income_chain",
                        lambda c, a=None: "sepolia" not in str(c))
    told = []

    async def _buyer(container, order, text, **k):
        told.append(text)
        return False
    monkeypatch.setattr(service, "tell_buyer", _buyer)
    o = ords.create(offer_id="a", offer_version=1, user_id="owner", channel="a2a", buyer="b",
                    inputs={}, amount_usd="5", spend_budget=0, now=1000.0)
    ords.attach_invoice(o.id, "r_1", "5", now=1000.0)
    n = await service._expire_unpayable(None, None, 1000.0 + 10 ** 6, _Db(chain))
    if expired:
        assert n == 1 and ords.get(o.id).status == ords.EXPIRED
        assert told == [f"Order {o.id} expired: a test-network payment does not buy."]
    else:
        assert n == 0 and ords.get(o.id).status == ords.AWAITING and told == []
