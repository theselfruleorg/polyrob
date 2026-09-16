"""046 T3: the credit a failed paid action writes is genuinely spendable.

⚠️ `OfferStore.redeemable_credit` had ZERO callers. The failure path told the
payer "you hold credit cr_… , good for one mute in this room at no further
charge" and nothing in the tree could honour it — while, because the apply seam
was unwired in production, that was the outcome of EVERY paid action.
"""
import os
import time

import pytest

from core.surfaces import room_actions as ra
from core.surfaces.room_action_store import Offer, OfferStore, store_path


# --- async-seam shim (2026-09-15) -------------------------------------------
# `offer()`/`apply()` became `async def` so a Telegram read runs on the CALLER's
# event loop rather than a bridged one ("got Future attached to a different
# loop"). These tests drive them synchronously.
import asyncio as _aio
from core.surfaces import room_actions as _ra


def _sync_seam(fn):
    def _call(*a, **k):
        r = fn(*a, **k)
        return _aio.run(r) if _aio.iscoroutine(r) else r
    return _call


offer = _sync_seam(_ra.offer)



@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "5150")
    monkeypatch.delenv("PAYMENT_DEFAULT_ASSET", raising=False)
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")

    from core.payments.assets import AssetStore, PaymentAsset
    from core.payments.assets import store_path as asset_path
    AssetStore(asset_path(str(tmp_path))).upsert(PaymentAsset(
        asset_id="usdcx", chain="base", address="0x" + "cc" * 20, decimals=6,
        symbol="USDC", rail="onchain_scan", source="operator"))

    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-100123")

    from core.surfaces import chat_policy
    for k, v in (("chat.paid_enabled", True), ("chat.paid_asset", "usdcx"),
                 ("chat.paid_mute_usd", 0.50)):
        ok, msg = chat_policy.set(str(tmp_path), "u_owner", "telegram",
                                  "-100123", k, v)
        assert ok, msg

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, n):
            return None

    return _C()


def _credit_row(rig, *, requester="2277", verb="mute", offer_id="off_old"):
    store = OfferStore(store_path(rig.config.data_dir))
    store.create(Offer(
        offer_id=offer_id, surface="telegram", chat_id="-100123", verb=verb,
        target_user_id="9911", target_name="S", duration_sec=3600,
        price_usd=0.5, asset_id="usdcx", amount_raw="500000",
        requester_id=requester, invoice_id="inv_old", status="credited",
        reason="the chat rejected it", created_at=time.time(),
        credit_id="cr_1"))
    return store


def _offer(rig, **kw):
    base = dict(surface="telegram", chat_id="-100123", verb="mute",
                target_user_id="9911", target_name="S", requester_id="2277",
                duration="1h", rights_fn=lambda s, c: {"can_restrict_members"},
                mint_fn=_boom_mint)
    base.update(kw)
    return offer(rig, **base)


def _offer_async(rig, **kw):
    """The un-shimmed coroutine, for the ASYNC test below — `asyncio.run`
    cannot be called from a running loop."""
    base = dict(surface="telegram", chat_id="-100123", verb="mute",
                target_user_id="9911", target_name="S", requester_id="2277",
                duration="1h", rights_fn=lambda s, c: {"can_restrict_members"},
                mint_fn=_boom_mint)
    base.update(kw)
    return _ra.offer(rig, **base)


def _boom_mint(**_kw):
    raise AssertionError("a redeemed credit must never mint an invoice")


def test_a_held_credit_is_spent_instead_of_minting(rig):
    store = _credit_row(rig)
    res = _offer(rig)
    assert res.ok and res.reason_code == "credit_redeemed", res.text
    assert "credit" in res.text
    assert store.get("off_old").status == "redeemed"
    fresh = store.get(res.offer_id)
    assert fresh.status == "paid" and fresh.price_usd == 0.0
    assert fresh.invoice_id == ""


def test_a_credit_is_consumed_exactly_once_under_two_attempts(rig):
    store = _credit_row(rig)
    first = _offer(rig)
    assert first.reason_code == "credit_redeemed"
    # The second attempt finds no credit and falls through to the paid path,
    # which this rig refuses at the mint (proving it got that far).
    second = _offer(rig)
    assert second.reason_code != "credit_redeemed"
    assert store.get("off_old").redeemed_by_offer == first.offer_id


def test_a_credit_for_another_verb_is_not_redeemable(rig):
    _credit_row(rig, verb="ban")
    res = _offer(rig)
    assert res.reason_code != "credit_redeemed"


def test_a_credit_another_payer_holds_is_not_redeemable(rig):
    _credit_row(rig, requester="9999")
    res = _offer(rig)
    assert res.reason_code != "credit_redeemed"


def test_a_credit_never_buys_its_way_past_the_rights_check(rig):
    """⚠️ Ordering. The rights probe runs BEFORE redemption, so a credit is
    never consumed into an action we cannot perform."""
    store = _credit_row(rig)
    res = _offer(rig, rights_fn=lambda s, c: set())
    assert not res.ok and res.reason_code == "missing_right"
    assert store.get("off_old").status == "credited", "the credit was spent"


def test_a_credit_never_buys_its_way_past_a_protected_target(rig):
    store = _credit_row(rig)
    res = _offer(rig, target_user_id="5150")      # the owner
    assert not res.ok and res.reason_code == "protected_target"
    assert store.get("off_old").status == "credited"


@pytest.mark.asyncio
async def test_a_failed_REDEEMED_action_does_not_claim_it_took_payment(rig):
    """⚠️ No money changed hands on a redemption, so "I took payment for mute"
    would be a confident wrong sentence — the class this whole review was
    about. The credit still rolls forward, which is the part the payer needs."""
    _credit_row(rig)
    res = await _offer_async(rig)
    assert res.reason_code == "credit_redeemed"

    async def boom(row, eff, until_ts):
        raise RuntimeError("CHAT_ADMIN_REQUIRED")

    out = await ra.apply(rig, res.offer_id, perform_fn=boom)
    assert not out.ok
    assert "I took payment" not in out.text
    assert "used your credit" in out.text
    assert "good for one mute" in out.text
