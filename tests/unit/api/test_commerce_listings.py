"""099 W6: HTTP / MCP / A2A listings — a caller sees only what is listed there, and
reads only its own order."""
import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.commerce import orders as ords
from core.commerce import store as st
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))

    async def _fake_invoice(order, m, amount, asset, *, db=None, path=None):
        ords.attach_invoice(order.id, f"inv_{order.id[-6:]}", amount, path=path)
        return {"request_id": f"inv_{order.id[-6:]}", "amount_usd": float(amount),
                "amount_raw": 5003721, "chain": "base", "recipient": "0x" + "77" * 20,
                "asset_id": asset}
    monkeypatch.setattr("modules.commerce.service._invoice", _fake_invoice)


def _live(listings):
    d = copy.deepcopy(MANIFEST)
    d["listings"] = listings
    o = st.save_draft("owner", d)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))


INPUTS = {"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}


def _client():
    from api.commerce_endpoints import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_http_lists_only_http402_offers_and_guards_order_reads():
    _live(["a2a"])
    c = _client()
    assert c.get("/api/offers").json()["offers"] == []
    assert c.post("/api/offers/custom-art/orders", json={"inputs": INPUTS}).status_code == 422
    st.save_draft("owner", dict(copy.deepcopy(MANIFEST), listings=["http402"]))
    o = st.get("custom-art")
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    listed = c.get("/api/offers").json()
    assert listed["offers"][0]["id"] == "custom-art"
    assert listed["jsonld"][0]["@type"] == "Product"
    assert "playbook" not in str(listed) and "spend_usd" not in str(listed)
    r = c.post("/api/offers/custom-art/orders", json={"inputs": INPUTS}).json()
    assert r["status"] == "awaiting_payment" and r["payment"]["amount_raw"] == "5003721"
    assert c.get(f"/api/orders/{r['order_id']}", params={"token": "wrong"}).status_code == 404
    ok = c.get(f"/api/orders/{r['order_id']}", params={"token": r["order_token"]}).json()
    assert ok["status"] == "awaiting_payment"


@pytest.mark.asyncio
async def test_mcp_orders_and_reads_only_its_own():
    from api.commerce_endpoints import mcp_call
    _live(["mcp"])
    listed = await mcp_call("polyrob_list_offers", {}, "u_ext")
    assert "custom-art" in listed["content"][0]["text"]
    made = await mcp_call("polyrob_create_order", {"offer_id": "custom-art", "inputs": INPUTS},
                          "u_ext")
    assert not made["isError"]
    order_id = ords.list_orders(channel="mcp")[0].id
    mine = await mcp_call("polyrob_order_status", {"order_id": order_id}, "u_ext")
    other = await mcp_call("polyrob_order_status", {"order_id": order_id}, "u_other")
    assert not mine["isError"] and other["isError"]


@pytest.mark.asyncio
async def test_a2a_data_part_orders_and_plain_text_is_untouched():
    from api.commerce_endpoints import a2a_order, a2a_skills
    _live(["a2a"])
    assert [s.id for s in a2a_skills()] == ["offer:custom-art"]
    assert await a2a_order({"message": {"parts": [{"kind": "text", "text": "hi"}]}}, "u") is None
    reply = await a2a_order({"message": {"parts": [{"kind": "data", "data": {
        "offer_id": "custom-art", "inputs": INPUTS}}]}}, "u")
    data = reply["parts"][1]["data"]
    assert data["status"] == "awaiting_payment" and data["payment"]["request_id"]
