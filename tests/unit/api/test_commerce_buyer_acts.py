"""F10: an HTTP, MCP or A2A buyer cancels, accepts and disputes ITS order — the same
`modules.commerce.service.buyer_act` the chat `/order <id> <act>` runs."""
from unittest.mock import AsyncMock

import pytest

from core.commerce import orders as ords
from tests.unit.api.test_commerce_listings import (  # noqa: F401 (autouse fixture)
    INPUTS, _client, _live, _store)


@pytest.fixture(autouse=True)
def _no_invoice_store(monkeypatch):
    monkeypatch.setattr("modules.commerce.service._close_invoice",
                        AsyncMock(return_value=None))


def _order(c):
    _live(["http402"])
    r = c.post("/api/offers/custom-art/orders", json={"inputs": INPUTS}).json()
    return r["order_id"], r["order_token"]


def _delivered(order_id):
    o = ords.get(order_id)
    ords.mark_paid(o.request_id, payment_tx="0x" + "cd" * 32)
    assert ords.claim_fulfillment(order_id)
    ords.deliver(order_id, {"url": "https://example.com/art.png"})
    assert ords.get(order_id).status == ords.DELIVERED


def test_the_buyer_cancels_an_unpaid_order():
    c = _client()
    oid, tok = _order(c)
    r = c.post(f"/api/orders/{oid}/cancel", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ords.CANCELLED
    again = c.post(f"/api/orders/{oid}/cancel", headers={"Authorization": f"Bearer {tok}"})
    assert again.status_code == 409 and "only an unpaid order" in again.json()["detail"]


def test_the_buyer_accepts_only_a_delivered_order():
    c = _client()
    oid, tok = _order(c)
    early = c.post(f"/api/orders/{oid}/accept", headers={"Authorization": f"Bearer {tok}"})
    assert early.status_code == 409
    _delivered(oid)
    r = c.post(f"/api/orders/{oid}/accept", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ords.ACCEPTED


def test_the_buyer_disputes_a_delivered_order_and_the_owner_is_told(monkeypatch):
    told = AsyncMock()
    monkeypatch.setattr("modules.commerce.service.owner_notice", told)
    c = _client()
    oid, tok = _order(c)
    _delivered(oid)
    r = c.post(f"/api/orders/{oid}/dispute", headers={"Authorization": f"Bearer {tok}"},
               json={"reason": "wrong colour"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ords.DISPUTED
    assert "wrong colour" in told.await_args.args[2]


def test_only_the_token_holder_acts_and_only_known_acts():
    c = _client()
    oid, tok = _order(c)
    assert c.post(f"/api/orders/{oid}/cancel",
                  headers={"Authorization": "Bearer wrong"}).status_code == 404
    assert c.post(f"/api/orders/{oid}/refund",
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 404
    assert ords.get(oid).status == ords.AWAITING


def test_the_buyer_act_routes_are_public():
    from api.auth_constants import is_public_path
    assert is_public_path("/api/orders/ord_x/cancel")


@pytest.mark.asyncio
async def test_chat_accept_runs_the_same_act():
    from modules.commerce.storefront import _order as chat_order
    c = _client()
    oid, _tok = _order(c)
    _delivered(oid)
    o = ords.get(oid)
    text = await chat_order("http", o.buyer, [oid, "accept"])
    assert "accepted" in text and ords.get(oid).status == ords.ACCEPTED


# -- MCP and A2A: the same act, proven by the authenticated caller ------------------------

async def _mcp_order(user="u_ext"):
    from api.commerce_endpoints import mcp_call
    _live(["mcp"])
    await mcp_call("polyrob_create_order", {"offer_id": "custom-art", "inputs": INPUTS}, user)
    return ords.list_orders(channel="mcp")[0].id


@pytest.mark.asyncio
async def test_mcp_buyer_acts_through_the_same_function(monkeypatch):
    import json

    from api.commerce_endpoints import MCP_TOOLS, mcp_call
    from api.mcp_serve.handlers import _TOOL_DISPATCH
    assert "polyrob_order_act" in {t["name"] for t in MCP_TOOLS} & set(_TOOL_DISPATCH)
    told = AsyncMock()
    monkeypatch.setattr("modules.commerce.service.owner_notice", told)
    oid = await _mcp_order()
    other = await mcp_call("polyrob_order_act", {"order_id": oid, "act": "cancel"}, "u_other")
    assert other["isError"] and "no such order" in other["content"][0]["text"]
    early = await mcp_call("polyrob_order_act", {"order_id": oid, "act": "accept"}, "u_ext")
    assert early["isError"] and "only a delivered order" in early["content"][0]["text"]
    _delivered(oid)
    r = await mcp_call("polyrob_order_act",
                       {"order_id": oid, "act": "dispute", "reason": "wrong colour"}, "u_ext")
    assert not r["isError"], r
    assert json.loads(r["content"][0]["text"])["status"] == ords.DISPUTED
    assert "wrong colour" in told.await_args.args[2]


@pytest.mark.asyncio
async def test_mcp_buyer_cancels_an_unpaid_order():
    from api.commerce_endpoints import mcp_call
    oid = await _mcp_order()
    r = await mcp_call("polyrob_order_act", {"order_id": oid, "act": "cancel"}, "u_ext")
    assert not r["isError"] and ords.get(oid).status == ords.CANCELLED


def _a2a(data):
    return {"message": {"parts": [{"kind": "data", "data": data}]}}


@pytest.mark.asyncio
async def test_a2a_buyer_acts_through_the_same_function():
    from api.commerce_endpoints import a2a_order
    _live(["a2a"])
    made = await a2a_order(_a2a({"offer_id": "custom-art", "inputs": INPUTS}), "u")
    oid = made["parts"][1]["data"]["order_id"]
    other = await a2a_order(_a2a({"order_id": oid, "act": "cancel"}), "u_other")
    assert other["parts"][1]["data"] == {"error": "no such order of yours"}
    early = await a2a_order(_a2a({"order_id": oid, "act": "accept"}), "u")
    assert "only a delivered order" in early["parts"][1]["data"]["error"]
    _delivered(oid)
    ok = await a2a_order(_a2a({"order_id": oid, "act": "accept"}), "u")
    assert ok["parts"][1]["data"]["status"] == ords.ACCEPTED
    assert ords.get(oid).status == ords.ACCEPTED
