"""Anonymous machine buyers (HTTP, MCP, A2A, a marketplace) cannot pile up unpaid
orders on one offer: at MAX_UNPAID_MACHINE_ORDERS the next machine order refuses, so
same-price pending invoices cannot use up the unique-amount space. A chat buyer and a
public-thread buyer are not affected."""
import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import service
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tests.unit.modules.commerce.test_order_service_e2e import _db, _env  # noqa: F401

IN = {"delivery_address": "0x" + "ab" * 20, "brief": "a cat"}


@pytest.mark.asyncio
async def test_machine_buyers_stop_at_the_unpaid_cap(tmp_path):
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", MANIFEST)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        for i in range(service.MAX_UNPAID_MACHINE_ORDERS):
            ords.create(offer_id=o.id, offer_version=1, user_id="owner",
                        channel=("http", "mcp", "a2a")[i % 3], buyer=f"m{i}", inputs=IN,
                        amount_usd="5", spend_budget=0)
        # chat and thread orders never count toward the machine cap
        ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="x",
                    buyer="t", inputs=IN, amount_usd="5", spend_budget=0, thread_ref="9")
        assert ords.count_unpaid(o.id, exclude_channels=("telegram",)) == \
            service.MAX_UNPAID_MACHINE_ORDERS
        with pytest.raises(service.OrderRefused, match="too many unpaid orders"):
            await service.create_order(o.id, channel="a2a", buyer="fresh", inputs=IN, db=db)
        order, _ = await service.create_order(o.id, channel="telegram", buyer="c", inputs=IN,
                                              db=db)
        assert order.status == ords.AWAITING
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_paid_machine_orders_do_not_count(tmp_path):
    db = await _db(tmp_path)
    try:
        o = st.save_draft("owner", MANIFEST)
        st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
        for i in range(service.MAX_UNPAID_MACHINE_ORDERS):
            od = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="http",
                             buyer=f"m{i}", inputs=IN, amount_usd="5", spend_budget=0)
            ords.attach_invoice(od.id, f"r{i}", "5")
            ords.mark_paid(f"r{i}", now=1000.0)          # long ago: outside the per_day window
        order, _ = await service.create_order(o.id, channel="a2a", buyer="fresh", inputs=IN,
                                              db=db)
        assert order.status == ords.AWAITING
    finally:
        await db.close()
