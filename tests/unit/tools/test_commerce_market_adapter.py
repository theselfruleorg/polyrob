"""099 W7: a configured MCP marketplace — publish the projection only, pull its orders
into OUR invoices, never twice for one marketplace reference."""
import copy
import json

import pytest

from core.commerce import hooks
from core.commerce import orders as ords
from core.commerce import store as st
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tools.commerce import market_adapter as ma


class _FakeMarket:
    def __init__(self):
        self.calls = []
        self.orders = [{"offer_id": "custom-art", "ref": "mk-1", "buyer": "alice",
                        "inputs": {"delivery_address": "0x" + "ab" * 20, "brief": "x"}}]

    async def __call__(self, server, tool, arguments):
        self.calls.append((server, tool, arguments))
        if tool == "list_new_orders":
            return json.dumps({"orders": self.orders})
        return "listing-77"


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))

    async def _fake_invoice(order, m, amount, asset, *, db=None, path=None):
        ords.attach_invoice(order.id, f"inv_{order.id[-6:]}", amount, path=path)
        return {"request_id": f"inv_{order.id[-6:]}", "amount_usd": float(amount)}
    monkeypatch.setattr("modules.commerce.service._invoice", _fake_invoice)
    yield
    hooks.unregister_listing_adapter("market:acme")


@pytest.mark.asyncio
async def test_publish_projection_and_sync_orders_once(tmp_path):
    (tmp_path / ma.CONFIG_NAME).write_text(json.dumps([{
        "name": "acme", "server": "acme", "publish_tool": "create_listing",
        "orders_tool": "list_new_orders", "reply_tool": "send_payment_request"}]))
    assert ma.load_config(str(tmp_path))[0]["name"] == "acme"
    fake = _FakeMarket()
    adapter = ma.MarketAdapter(ma.load_config(str(tmp_path))[0], fake)
    hooks.register_listing_adapter(adapter)
    d = copy.deepcopy(MANIFEST)
    d["listings"] = ["market:acme"]
    o = st.save_draft("owner", d)
    from modules.commerce.owner_verb import offer_reply
    from unittest.mock import patch
    with patch("modules.commerce.owner_verb._promote_playbook", return_value="sha"):
        text = await offer_reply("owner", ["live", o.id, st.manifest_hash(o.manifest)])
    assert "market:acme: published (listing-77)" in text
    published = fake.calls[0][2]["offer"]
    assert published["id"] == "custom-art" and "playbook" not in json.dumps(published)
    from modules.commerce.service import tick
    assert (await tick())["synced"] == 1
    assert (await tick())["synced"] == 0          # the same marketplace ref is not re-ordered
    order = ords.by_source("market:acme:mk-1")
    assert order.channel == "market:acme" and order.status == "awaiting_payment"
    assert fake.calls[-1][1] == "list_new_orders"
    assert any(c[1] == "send_payment_request" for c in fake.calls)
