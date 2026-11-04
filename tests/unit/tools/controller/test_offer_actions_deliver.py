"""099 review C3: after a retry only the order's CURRENT goal may deliver or fail it."""
from types import SimpleNamespace

import pytest

from core.autonomy_marker import mark_autonomous
from core.commerce import orders as ords
from core.commerce import store as st
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tools.controller.offer_actions import _deliver_refusal


@pytest.fixture
def order(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))
    o = st.save_draft("owner", MANIFEST)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))
    order = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="a2a",
                        buyer="b", inputs={"delivery_address": "0x" + "ab" * 20},
                        amount_usd="5.00", spend_budget=0.5)
    ords.attach_invoice(order.id, "inv_c3", "5.00")
    ords.mark_paid("inv_c3")
    ords.set_goal(order.id, ords.claim_fulfillment(order.id), "g_old")
    return order


def _ctx(order_id, session):
    return SimpleNamespace(user_id="owner", session_id=session, metadata={"mandate": order_id},
                           is_sub_agent=False, role="orchestrator")


def test_the_current_goal_may_deliver_and_a_retried_one_may_not(order):
    ctrl = SimpleNamespace(orchestrator=None)
    mark_autonomous("s_old", "g_old")
    assert _deliver_refusal(ctrl, _ctx(order.id, "s_old"), order.id) is None
    # overdue -> retry: a new goal owns the order now
    ords.release_for_retry(order.id)
    ords.set_goal(order.id, ords.claim_fulfillment(order.id), "g_new")
    mark_autonomous("s_new", "g_new")
    why = _deliver_refusal(ctrl, _ctx(order.id, "s_old"), order.id)
    assert why and "no longer" in why
    assert _deliver_refusal(ctrl, _ctx(order.id, "s_new"), order.id) is None
