"""Offers are durable rows; a status transition is never a process attribute."""
import time

import pytest

from core.surfaces.room_action_store import Offer, OfferStore


@pytest.fixture()
def store(tmp_path):
    return OfferStore(str(tmp_path / "room_actions.db"))


def _offer(**kw):
    base = dict(offer_id="off_1", surface="telegram", chat_id="-100123",
                verb="mute", target_user_id="9911", target_name="someone",
                duration_sec=3600, price_usd=0.50, asset_id="rob",
                amount_raw="1000000000000000000", requester_id="2277",
                invoice_id="", status="pending", reason="",
                created_at=time.time(), settled_at=0.0, applied_at=0.0,
                credit_id="")
    base.update(kw)
    return Offer(**base)


def test_a_created_offer_reads_back_identical(store):
    offer = _offer(created_at=1_700_000_000.5)
    store.create(offer)
    assert store.get("off_1") == offer


def test_amount_raw_survives_18_decimals_as_text(store):
    """⚠️ An 18-decimal amount exceeds SQLite's signed 64-bit INTEGER range."""
    raw = str(7 * 10 ** 30 + 3)
    store.create(_offer(amount_raw=raw))
    assert store.get("off_1").amount_raw == raw


def test_set_status_moves_the_row_and_returns_true(store):
    store.create(_offer())
    assert store.set_status("off_1", "paid", settled_at=123.0) is True
    row = store.get("off_1")
    assert row.status == "paid" and row.settled_at == 123.0


def test_set_status_on_an_unknown_offer_is_false_not_an_exception(store):
    assert store.set_status("nope", "paid") is False


def test_an_unknown_status_is_refused_at_write(store):
    store.create(_offer())
    with pytest.raises(ValueError, match="unknown status"):
        store.set_status("off_1", "vaporized")


def test_by_invoice_finds_the_offer_settlement_must_actuate(store):
    """⚠️ The ONLY link from a settlement back to the effect it paid for."""
    store.create(_offer(invoice_id="inv_abc"))
    assert store.by_invoice("inv_abc").offer_id == "off_1"
    assert store.by_invoice("inv_zzz") is None
    assert store.by_invoice("") is None
    assert store.by_invoice(None) is None


def test_count_since_is_scoped_to_the_room_and_the_person(store):
    now = time.time()
    store.create(_offer(offer_id="a", requester_id="1", target_user_id="9"))
    store.create(_offer(offer_id="b", requester_id="1", target_user_id="8"))
    store.create(_offer(offer_id="c", requester_id="2", target_user_id="9"))
    store.create(_offer(offer_id="d", chat_id="-100999", requester_id="1",
                        target_user_id="9"))
    assert store.count_since("telegram", "-100123", requester="1",
                             since_ts=now - 60) == 2
    assert store.count_since("telegram", "-100123", target="9",
                             since_ts=now - 60) == 2
    assert store.count_since("telegram", "-100123", requester="1",
                             since_ts=now + 60) == 0


def test_a_refused_offer_does_not_count_against_a_cap(store):
    """⚠️ A refusal did not happen to anyone. Counting it would let three
    refused attempts lock a member out for the day."""
    now = time.time()
    store.create(_offer(offer_id="a", requester_id="1"))
    store.create(_offer(offer_id="b", requester_id="1", status="refused"))
    assert store.count_since("telegram", "-100123", requester="1",
                             since_ts=now - 60) == 1


def test_expire_stale_moves_only_pending_rows_past_the_ttl(store):
    now = time.time()
    store.create(_offer(offer_id="old", created_at=now - 7200))
    store.create(_offer(offer_id="new", created_at=now - 60))
    store.create(_offer(offer_id="paid_old", created_at=now - 7200,
                        status="paid"))
    assert store.expire_stale(1800, now=now) == 1
    assert store.get("old").status == "expired"
    assert store.get("new").status == "pending"
    assert store.get("paid_old").status == "paid"


def test_credits_owed_lists_only_credited_rows(store):
    store.create(_offer(offer_id="a", status="applied"))
    store.create(_offer(offer_id="b", status="credited", credit_id="cr_1"))
    assert [o.offer_id for o in store.credits_owed()] == ["b"]


def test_a_redeemable_credit_is_found_per_payer_room_and_verb(store):
    store.create(_offer(offer_id="a", status="credited", credit_id="cr_1"))
    assert store.redeemable_credit("telegram", "-100123", "2277",
                                   "mute").offer_id == "a"
    assert store.redeemable_credit("telegram", "-100123", "9999", "mute") is None
    assert store.redeemable_credit("telegram", "-100123", "2277", "ban") is None
    assert store.redeemable_credit("telegram", "-100999", "2277", "mute") is None


def test_open_offers_lists_only_pending_rows_for_this_room(store):
    store.create(_offer(offer_id="a"))
    store.create(_offer(offer_id="b", status="applied"))
    store.create(_offer(offer_id="c", chat_id="-100999"))
    assert [o.offer_id for o in store.open_offers("telegram", "-100123")] == ["a"]


def test_the_sidecar_db_is_in_the_backup_manifest():
    from core.db_manifest import SIDECAR_DB_NAMES
    assert "room_actions.db" in SIDECAR_DB_NAMES


def test_pending_rooms_lists_each_room_with_an_unpaid_offer(store):
    """The expiry sweep needs this because the TTL is a PER-ROOM setting: one
    global cutoff would silently override every room's own choice."""
    store.create(_offer(offer_id="a", chat_id="-1"))
    store.create(_offer(offer_id="b", chat_id="-1"))
    store.create(_offer(offer_id="c", chat_id="-2"))
    store.create(_offer(offer_id="d", chat_id="-3", status="applied"))
    assert sorted(store.pending_rooms()) == [("telegram", "-1"),
                                             ("telegram", "-2")]


def test_expire_stale_can_be_scoped_to_one_room(store):
    now = 1_000_000.0
    store.create(_offer(offer_id="a", chat_id="-1", created_at=now - 7200))
    store.create(_offer(offer_id="b", chat_id="-2", created_at=now - 7200))
    assert store.expire_stale(1800, surface="telegram", chat_id="-1",
                              now=now) == 1
    assert store.get("a").status == "expired"
    assert store.get("b").status == "pending"
