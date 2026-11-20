"""L-19 (audit 2026-11-21): one anonymous origin cannot fill the shared machine pool.
An HTTP buyer gets a fresh token per order, so the cap counts per client address
(the seat passes it as ``origin``). Past MAX_UNPAID_PER_ORIGIN that address refuses,
while another address still orders. An MCP or A2A caller is one buyer identity, which
the offer's ``open_per_buyer`` cap (default 1) already holds."""
import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import service
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tests.unit.modules.commerce.test_order_service_e2e import _db, _env  # noqa: F401

IN = {"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}


def _go_live():
    o = st.save_draft("owner", MANIFEST)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    return o


@pytest.mark.asyncio
async def test_one_http_address_stops_at_its_share(tmp_path):
    db = await _db(tmp_path)
    try:
        o = _go_live()
        assert service.MAX_UNPAID_PER_ORIGIN < service.MAX_UNPAID_MACHINE_ORDERS
        for i in range(service.MAX_UNPAID_PER_ORIGIN):
            await service.create_order(o.id, channel="http", buyer=f"tok{i}", inputs=IN,
                                       db=db, origin="198.51.100.7")
        with pytest.raises(service.OrderRefused, match="too many unpaid orders"):
            await service.create_order(o.id, channel="http", buyer="tok-new", inputs=IN,
                                       db=db, origin="198.51.100.7")
        other, _ = await service.create_order(o.id, channel="http", buyer="tok-o", inputs=IN,
                                              db=db, origin="203.0.113.9")
        assert other.status == ords.AWAITING
        # the stored origin is a key, never the raw address
        assert all("198.51.100.7" not in (x.source or "")
                   for x in ords.list_orders(offer_id=o.id, limit=100))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_mcp_caller_is_held_by_open_per_buyer(tmp_path):
    db = await _db(tmp_path)
    try:
        o = _go_live()
        await service.create_order(o.id, channel="mcp", buyer="mcp:u1", inputs=IN, db=db)
        with pytest.raises(service.OrderRefused, match="open order"):
            await service.create_order(o.id, channel="mcp", buyer="mcp:u1", inputs=IN, db=db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chat_buyers_have_no_origin_cap(tmp_path):
    db = await _db(tmp_path)
    try:
        o = _go_live()
        for i in range(service.MAX_UNPAID_PER_ORIGIN + 1):
            await service.create_order(o.id, channel="telegram", buyer=f"c{i}", inputs=IN,
                                       db=db, origin="198.51.100.7")
    finally:
        await db.close()
