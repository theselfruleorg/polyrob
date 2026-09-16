"""Settlement actuates the effect. A failure pays a CREDIT, never cash."""
import os
import time

import pytest

from core.surfaces import room_actions as ra
from core.surfaces.room_action_store import Offer, OfferStore, store_path


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")
    # 046 T5: the owner is resolved by their ADDRESS on the surface. Without one
    # configured, `_target_protection` refuses the whole sale rather than
    # guessing — so an apply-path test must name an owner too.
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "5150")
    store = OfferStore(store_path(str(tmp_path)))
    store.create(Offer(
        offer_id="off_1", surface="telegram", chat_id="-100123", verb="mute",
        target_user_id="9911", target_name="S", duration_sec=3600,
        price_usd=0.5, asset_id="rob", amount_raw=str(5 * 10 ** 18),
        requester_id="2277", invoice_id="inv_1", status="paid", reason="",
        created_at=time.time(), settled_at=time.time()))

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, n):
            return None

    return _C(), store


class _Ok:
    ok = True
    reason = ""


class _Fail:
    ok = False
    reason = "CHAT_ADMIN_REQUIRED"


def _performer(result=None, calls=None):
    async def perform(row, eff, until_ts):
        if calls is not None:
            calls.append((row.offer_id, eff.verb, until_ts))
        return result or _Ok()
    return perform


@pytest.mark.asyncio
async def test_a_successful_apply_performs_and_marks_the_row(rig):
    container, store = rig
    calls = []
    res = await ra.apply(container, "off_1", perform_fn=_performer(calls=calls))
    assert res.ok, res.text
    assert calls and calls[0][1] == "mute"
    assert store.get("off_1").status == "applied"


@pytest.mark.asyncio
async def test_the_until_timestamp_is_the_offers_own_duration(rig):
    container, _ = rig
    calls = []
    await ra.apply(container, "off_1", perform_fn=_performer(calls=calls),
                   now=1_000_000.0)
    assert calls[0][2] == 1_000_000 + 3600


@pytest.mark.asyncio
async def test_apply_is_idempotent_on_the_offer_id(rig):
    container, _ = rig
    calls = []
    perform = _performer(calls=calls)
    await ra.apply(container, "off_1", perform_fn=perform)
    res = await ra.apply(container, "off_1", perform_fn=perform)
    assert res.ok
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_unknown_offer_is_reported_not_crashed(rig):
    container, _ = rig
    res = await ra.apply(container, "nope", perform_fn=_performer())
    assert not res.ok and res.reason_code == "unknown"


@pytest.mark.asyncio
async def test_a_target_promoted_to_admin_between_mint_and_settlement_is_spared(rig):
    """⚠️ Protections are RE-CHECKED at apply. A grant made after the mint must
    win, or a paid offer outlives the authority it was aimed at."""
    container, store = rig
    from core.surfaces.group_roles import GroupRoles
    GroupRoles(os.path.join(container.config.data_dir, "surfaces.db")).grant(
        "telegram", "-100123", "9911", "admin", granted_by="u_owner")
    calls = []
    res = await ra.apply(container, "off_1", perform_fn=_performer(calls=calls))
    assert not res.ok
    assert calls == [], "the effect was performed against a protected target"
    assert store.get("off_1").status == "credited"


@pytest.mark.asyncio
async def test_a_telegram_failure_pays_a_credit_and_never_a_refund(rig):
    container, store = rig
    res = await ra.apply(container, "off_1",
                         perform_fn=_performer(result=_Fail()))
    assert not res.ok
    row = store.get("off_1")
    assert row.status == "credited"
    assert row.credit_id
    assert "CHAT_ADMIN_REQUIRED" in row.reason
    assert "refund" not in res.text.lower()
    assert "credit" in res.text.lower()


@pytest.mark.asyncio
async def test_a_raising_performer_pays_a_credit_too(rig):
    container, store = rig

    async def boom(row, eff, until_ts):
        raise RuntimeError("network gone")

    res = await ra.apply(container, "off_1", perform_fn=boom)
    assert not res.ok
    assert store.get("off_1").status == "credited"
    assert "network gone" in store.get("off_1").reason


@pytest.mark.asyncio
async def test_no_transport_at_all_pays_a_credit(rig):
    container, store = rig
    res = await ra.apply(container, "off_1", perform_fn=None)
    assert not res.ok
    assert store.get("off_1").status == "credited"


@pytest.mark.asyncio
async def test_an_apply_runs_even_while_autonomy_is_paused(rig):
    """⚠️ We already hold the payer's money. A pause must not strand an
    obligation we took payment for."""
    container, store = rig
    from core import autonomy_control as ac
    ac.pause(container.config.data_dir, scopes=("all",), set_by="t", via="t")
    res = await ra.apply(container, "off_1", perform_fn=_performer())
    assert res.ok
    assert store.get("off_1").status == "applied"


@pytest.mark.asyncio
async def test_credits_owed_surfaces_the_failure_for_the_owner(rig):
    container, store = rig
    await ra.apply(container, "off_1", perform_fn=_performer(result=_Fail()))
    assert [o.offer_id for o in store.credits_owed()] == ["off_1"]
    assert store.redeemable_credit("telegram", "-100123", "2277",
                                   "mute").offer_id == "off_1"


@pytest.mark.asyncio
async def test_an_already_credited_offer_is_not_retried(rig):
    container, store = rig
    calls = []
    await ra.apply(container, "off_1", perform_fn=_performer(result=_Fail()))
    res = await ra.apply(container, "off_1", perform_fn=_performer(calls=calls))
    assert not res.ok and res.reason_code == "credited"
    assert calls == []


def test_mark_settled_records_the_money_before_the_effect_is_attempted(rig):
    """⚠️ A separate durable step: if the process dies between the money
    arriving and the effect landing, the row still says `paid` and the
    obligation is visible rather than lost."""
    container, store = rig
    store.set_status("off_1", "pending")
    assert ra.mark_settled(container, "off_1", now=42.0) is True
    row = store.get("off_1")
    assert row.status == "paid" and row.settled_at == 42.0


def test_the_receipt_names_the_undo_the_catalog_actually_offers(rig):
    _, store = rig
    row = store.get("off_1")
    text = ra.receipt(row, ra.effect("mute"))
    assert "S" in text and "muted" in text
    assert "/unmute" in text


def test_apply_is_not_pause_gated_and_offer_is():
    """⚠️ The asymmetry is the point, so it is pinned rather than trusted."""
    import inspect
    assert "allows(" in inspect.getsource(ra.offer)
    assert "allows(" not in inspect.getsource(ra.apply)
