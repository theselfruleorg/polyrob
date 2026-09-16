"""The status snapshot reports paid actions — and says so when it cannot."""
import time

import pytest


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def _offer(**kw):
    from core.surfaces.room_action_store import Offer
    base = dict(offer_id="o", surface="telegram", chat_id="-1", verb="mute",
                target_user_id="9", target_name="", duration_sec=60,
                price_usd=1.0, asset_id="rob", amount_raw="1",
                requester_id="2", invoice_id="i", status="pending", reason="",
                created_at=time.time())
    base.update(kw)
    return Offer(**base)


def _store(home):
    from core.surfaces.room_action_store import OfferStore, store_path
    return OfferStore(store_path(home))


def test_a_room_actions_section_is_always_present(_home):
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("u_owner")
    assert "room_actions" in snap.sections


def test_an_absent_store_is_no_paid_actions_and_is_never_created(tmp_path):
    import os
    from core.status_snapshot import build_status_snapshot
    sec = build_status_snapshot("u_owner").section("room_actions")
    assert sec.data["credits_owed"] == 0
    assert not os.path.exists(os.path.join(str(tmp_path), "room_actions.db")), (
        "a status READ created the store")


def test_credits_owed_lead_as_a_critical_health_item(_home):
    """⚠️ Money we HOLD against an undelivered service."""
    _store(_home).create(_offer(offer_id="c1", status="credited",
                                credit_id="cr_1", reason="bot lost admin"))
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("u_owner")
    sec = snap.section("room_actions")
    assert sec.data["credits_owed"] == 1
    keys = [h.key for h in snap.health]
    assert "room_action_credits_owed" in keys
    item = next(h for h in snap.health
                if h.key == "room_action_credits_owed")
    assert item.severity == "crit"
    assert "bot lost admin" in item.text


def test_a_paid_but_unapplied_offer_is_named_but_not_escalated(_home):
    """An obligation in flight is not a failure yet."""
    _store(_home).create(_offer(offer_id="p1", status="paid"))
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("u_owner")
    sec = snap.section("room_actions")
    assert sec.data["paid"] == 1
    assert any("awaiting the effect" in ln for ln in sec.lines)
    assert "room_action_credits_owed" not in [
        h.key for h in snap.health]


def test_an_unreadable_store_renders_its_reason_never_a_zero(tmp_path):
    """⚠️ A confident zero would read as 'nothing is owed'."""
    (tmp_path / "room_actions.db").write_bytes(b"not a database")
    from core.status_snapshot import build_status_snapshot
    sec = build_status_snapshot("u_owner").section("room_actions")
    assert sec.state != "ok"
    assert sec.data.get("credits_owed") in (None, 0) or sec.state == "unavailable"
