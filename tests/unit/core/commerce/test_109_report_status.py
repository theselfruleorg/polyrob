"""109 W6: the owner sees selling in /status and /offer report|threads."""
import pytest

from core.commerce import orders as ords
from core.commerce import report
from core.commerce import store as st
from core.commerce import thread
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))
    o = st.save_draft("owner", MANIFEST)
    return st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.manifest_hash(o.manifest))


def _paid(o, req):
    od = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="x", buyer=req,
                     inputs={}, amount_usd="3.30", spend_budget=0.2)
    ords.attach_invoice(od.id, req, "3.30")
    ords.mark_paid(req, payment_tx="0x" + "1" * 64)
    return od


def test_the_report_keeps_treasury_and_never_subtracts_llm(live):
    _paid(live, "i1")
    _paid(live, "i2")
    f = _paid(live, "i3")
    ords.fail(f.id, "deadline")
    ords.create(offer_id=live.id, offer_version=1, user_id="owner", channel="x", buyer="u",
                inputs={}, amount_usd="3.30", spend_budget=0.2)        # unpaid: no income
    text = report.offer_report("owner")
    assert "2 sold, income $6.60" in text and "1 failed paid order(s)" in text
    assert "to refund" not in text
    assert "never subtracted" in text
    assert report.offer_report("owner", "nope") == "No offer nope."


def test_status_names_open_orders_refunds_and_a_stopped_intake(live):
    _paid(live, "i1")
    f = _paid(live, "i2")
    ords.fail(f.id, "x")
    thread.announce(live.id, "x", "100")
    thread.save_cursor("x", "5", stop_reason="x api: 402 (credits depleted); replies wait",
                       now=1000.0)
    lines, health = report.status_lines("owner", now=1000.0 + 60)
    assert lines[0].startswith("selling: 1 live offer(s) of 1; open orders: 1 paid")
    assert any("x order replies: last read 1 min ago — x api: 402" in ln for ln in lines)
    keys = {k for k, _t, _r in health}
    assert "offer_threads_x" in keys
    # a refund owed is the invoice's refund_due (the money section's
    # payment_refund_due item), never counted a second time here
    assert "offer_refund_owed" not in keys


def test_no_offers_is_no_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "c.db"))
    assert report.status_lines("owner") == ([], [])


@pytest.mark.asyncio
async def test_the_owner_verb_has_report_and_threads(live):
    from modules.commerce.owner_verb import offer_reply
    assert "Offer report" in await offer_reply("owner", ["report"])
    assert "No announcement posts" in await offer_reply("owner", ["threads"])
    thread.announce(live.id, "x", "100")
    assert "x post 100" in await offer_reply("owner", ["threads"])
    assert "x post 100" not in await offer_reply("intruder", ["threads"])
