"""One helper set; every seat renders its verified sentence (046)."""
import time

import pytest

from core.surfaces import room_action_admin as adm


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")

    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(str(tmp_path))).upsert(PaymentAsset(
        asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="ROB", rail="onchain_scan", source="operator"))

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, n):
            return None

    return _C()


def _store(rig):
    from core.surfaces.room_action_store import OfferStore, store_path
    return OfferStore(store_path(rig.config.data_dir))


def _offer(**kw):
    from core.surfaces.room_action_store import Offer
    base = dict(offer_id="o1", surface="telegram", chat_id="-100123",
                verb="mute", target_user_id="9", target_name="S",
                duration_sec=3600, price_usd=1.0, asset_id="rob",
                amount_raw="1", requester_id="2", invoice_id="i",
                status="pending", reason="", created_at=time.time())
    base.update(kw)
    return Offer(**base)


def test_status_on_an_unconfigured_room_says_so_plainly(rig):
    out = adm.status(rig, "telegram", "-100123")
    assert "not enabled" in out.lower()
    assert "/paid price" in out


def test_enable_then_status_reports_the_price_and_the_asset(rig):
    assert "✅" in adm.set_asset(rig, "telegram", "-100123", "rob")
    assert "✅" in adm.set_price(rig, "telegram", "-100123", "mute", 0.5)
    assert "✅" in adm.enable(rig, "telegram", "-100123")
    out = adm.status(rig, "telegram", "-100123")
    assert "rob" in out and "0.50" in out


def test_status_warns_when_members_cannot_invoke_the_verb(rig):
    """⚠️ A priced, enabled room whose members cannot reach /mute looks
    working and refuses everyone."""
    adm.set_asset(rig, "telegram", "-100123", "rob")
    adm.set_price(rig, "telegram", "-100123", "mute", 0.5)
    adm.enable(rig, "telegram", "-100123")
    out = adm.status(rig, "telegram", "-100123")
    assert "member_verbs" in out


def test_set_price_on_an_unknown_verb_echoes_the_vocabulary(rig):
    out = adm.set_price(rig, "telegram", "-100123", "vaporize", 1.0)
    assert "mute" in out and "vaporize" in out


def test_set_price_refuses_a_non_positive_price(rig):
    """⚠️ Never a $0.00 verb — that is a free action wearing a price."""
    assert "❌" in adm.set_price(rig, "telegram", "-100123", "mute", 0.0)
    assert "❌" in adm.set_price(rig, "telegram", "-100123", "mute", -1.0)
    assert "❌" in adm.set_price(rig, "telegram", "-100123", "mute", "free")


def test_set_asset_to_an_unconfigured_id_refuses(rig):
    out = adm.set_asset(rig, "telegram", "-100123", "no-such")
    assert "no-such" in out
    assert "polyrob wallet asset" in out


def test_enable_without_a_price_refuses_rather_than_offering_a_free_action(rig):
    out = adm.enable(rig, "telegram", "-100123")
    assert "❌" in out and "priced" in out


def test_disable_says_what_changes_for_a_member(rig):
    out = adm.disable(rig, "telegram", "-100123")
    assert "refused, not priced" in out


def test_offers_lists_recent_rows_newest_first(rig):
    store = _store(rig)
    store.create(_offer(offer_id="a", created_at=100.0))
    store.create(_offer(offer_id="b", created_at=200.0, status="applied"))
    out = adm.offers(rig, "telegram", "-100123")
    # Match the offer-id markers, not a bare letter — "paid action(s)" is full
    # of stray a's and b's.
    assert out.index("(b)") < out.index("(a)")
    assert "applied" in out


def test_offers_on_an_empty_room_says_so(rig):
    assert "No paid actions" in adm.offers(rig, "telegram", "-100123")


def test_cancel_moves_a_pending_offer_to_refused(rig):
    _store(rig).create(_offer(offer_id="o1"))
    out = adm.cancel(rig, "o1", by="u_owner")
    assert "o1" in out and "✅" in out
    assert _store(rig).get("o1").status == "refused"


def test_cancel_refuses_a_PAID_offer(rig):
    """⚠️ Cancelling a paid offer would take the money and cancel the service.
    That case is a CREDIT, not a cancellation."""
    _store(rig).create(_offer(offer_id="o2", status="paid"))
    out = adm.cancel(rig, "o2", by="u_owner")
    assert "❌" in out and "credited" in out
    assert _store(rig).get("o2").status == "paid"


def test_cancel_of_an_unknown_offer_says_so(rig):
    assert "unknown offer" in adm.cancel(rig, "nope")


def test_render_credits_names_each_one_and_why(rig):
    _store(rig).create(_offer(offer_id="c1", status="credited",
                              credit_id="cr_1", reason="bot lost admin"))
    out = adm.render_credits(rig)
    assert "c1" in out and "bot lost admin" in out


def test_render_credits_on_a_clean_room_says_none_owed(rig):
    assert "No paid-action credits owed" in adm.render_credits(rig)


# --- 046 phase 2 ------------------------------------------------------------

def test_pricing_a_ban_actually_writes_a_key_something_reads(tmp_path, monkeypatch):
    """⚠️ `set_price` validated `ban` against the CATALOG and then wrote
    `chat.paid_ban_usd`, a key that did not exist in the schema — so an owner
    was told the price was set and every member was refused "ban has no price"."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")
    from core.surfaces import room_action_admin as adm
    from core.surfaces.chat_policy import load_for_chat

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, n):
            return None

    out = adm.set_price(_C(), "telegram", "-100123", "ban", 2.0)
    assert out.startswith("✅"), out
    assert "paid_ban_max_duration" in out, "the duration key is not discoverable"
    policy = load_for_chat(str(tmp_path), "telegram", "-100123")
    assert policy.paid_ban_usd == 2.0

    from core.surfaces.room_actions import _price_band
    assert _price_band(policy, "ban")[0] == 2.0


def test_status_lists_a_priced_ban_with_its_duration_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")
    from core.surfaces import chat_policy
    from core.surfaces import room_action_admin as adm

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, n):
            return None

    for k, v in (("chat.paid_enabled", True), ("chat.paid_ban_usd", 2.0),
                 ("chat.paid_ban_max_duration", "6h")):
        ok, msg = chat_policy.set(str(tmp_path), "u_owner", "telegram",
                                  "-100123", k, v)
        assert ok, msg
    out = adm.status(_C(), "telegram", "-100123")
    assert "ban: $2.00 (up to 6h)" in out


def test_status_warns_about_every_priced_verb_a_member_cannot_ask_for(
        tmp_path, monkeypatch):
    """⚠️ A room that prices `ban` and grants only `mute` sells something no
    member can ask for, and nothing said so — the warning checked `mute` alone."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")
    from core.surfaces import chat_policy
    from core.surfaces import room_action_admin as adm

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, n):
            return None

    for k, v in (("chat.paid_enabled", True), ("chat.paid_mute_usd", 0.5),
                 ("chat.paid_ban_usd", 2.0),
                 ("chat.member_verbs", ["help", "mute"])):
        ok, msg = chat_policy.set(str(tmp_path), "u_owner", "telegram",
                                  "-100123", k, v)
        assert ok, msg
    out = adm.status(_C(), "telegram", "-100123")
    assert "priced but NOT grantable" in out and "ban" in out
    assert "member_verbs ban,help,mute" in out
