"""The ONE mint path, and every refusal it can name (046 §4.7)."""
import inspect
import os
import time

import pytest

from core.surfaces import room_actions as ra


# --- async-seam shim (2026-09-15) -------------------------------------------
# `offer()` became `async def` so a Telegram read runs on the CALLER's event
# loop rather than a bridged one ("got Future attached to a different loop").
# These tests drive it synchronously.
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
    """A container-shaped object over an isolated data home: a room that is
    allowlisted and paid-enabled, priced in an 18-decimal operator asset."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    monkeypatch.delenv("PAYMENT_DEFAULT_ASSET", raising=False)

    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(str(tmp_path))).upsert(PaymentAsset(
        asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="ROB", rail="onchain_scan",
        min_amount_raw=10 ** 18, liquidity_floor_usd=2500.0, source="operator"))

    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-100123")

    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")
    # ⚠️ The owner is resolved by their ADDRESS on this surface, the way routing
    # resolves them (046 T5). `core.instance.is_owner(raw_telegram_id)` is a
    # PERMANENT False — no principal, `local=False` — so the pre-phase-2 rig
    # patched a function whose real call site could never have protected anyone,
    # and the owner was targetable in production while this test passed.
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "5150")

    from core.surfaces import chat_policy
    for k, v in (("chat.paid_enabled", True), ("chat.paid_asset", "rob"),
                 ("chat.paid_mute_usd", 0.50), ("chat.paid_mute_min_usd", 0.10),
                 ("chat.paid_mute_max_usd", 5.0),
                 ("chat.paid_mute_max_duration", "24h")):
        ok, msg = chat_policy.set(str(tmp_path), "u_owner", "telegram",
                                  "-100123", k, v)
        assert ok, msg

    class _C:
        class config:
            data_dir = str(tmp_path)

        def get_service(self, name):
            return None

    return _C()


def _quoter(price=0.10, liq=50_000.0, verdict="SURVIVOR", ts=None):
    from core.payments.quote import PriceQuote

    class _Q:
        def quote(self, asset):
            return PriceQuote("rob", price, liq, verdict, "test",
                              ts if ts is not None else time.time())
    return _Q()


def _rights(*granted):
    return lambda surface, chat_id: set(granted)


def _mint(**_kw):
    return {"request_id": "inv_test", "recipient": "0x" + "11" * 20}


def _offer(rig, **kw):
    base = dict(surface="telegram", chat_id="-100123", verb="mute",
                target_user_id="9911", target_name="someone",
                requester_id="2277", duration="1h",
                rights_fn=_rights("can_restrict_members"), quoter=_quoter(),
                mint_fn=_mint)
    base.update(kw)
    return offer(rig, **base)


def _store(rig):
    from core.surfaces.room_action_store import OfferStore, store_path
    return OfferStore(store_path(rig.config.data_dir))


# --- the happy path ---------------------------------------------------------

def test_a_configured_room_mints_an_offer(rig):
    res = _offer(rig)
    assert res.ok, res.text
    assert res.offer_id
    assert "ROB" in res.text


def test_the_offer_row_is_durable_and_pending_with_the_sized_amount(rig):
    res = _offer(rig)
    row = _store(rig).get(res.offer_id)
    assert row.status == "pending"
    assert row.verb == "mute" and row.target_user_id == "9911"
    assert row.invoice_id == "inv_test"
    assert int(row.amount_raw) == 5 * 10 ** 18       # $0.50 at $0.10/token


def test_the_offer_text_names_the_treasury_and_the_expiry(rig):
    res = _offer(rig)
    assert "0x" + "11" * 20 in res.text
    assert "inv_test" in res.text
    assert "30m" in res.text                          # the room's default ttl


# --- check 1: the flag, the allowlist, the room switch ----------------------

def test_the_instance_flag_off_refuses(rig, monkeypatch):
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "false")
    res = _offer(rig)
    assert not res.ok and res.reason_code == "disabled"


def test_a_room_with_paid_disabled_refuses(rig):
    from core.surfaces import chat_policy
    chat_policy.set(rig.config.data_dir, "u_owner", "telegram", "-100123",
                    "chat.paid_enabled", False)
    res = _offer(rig)
    assert not res.ok and res.reason_code == "disabled"


def test_an_unallowlisted_room_refuses(rig):
    res = _offer(rig, chat_id="-100999")
    assert not res.ok and res.reason_code == "disabled"


# --- check 2: the catalog ---------------------------------------------------

def test_an_unknown_verb_refuses_and_echoes_the_vocabulary(rig):
    res = _offer(rig, verb="vaporize")
    assert not res.ok and res.reason_code == "unknown_verb"
    assert "mute" in res.text


# --- check 3: the duration --------------------------------------------------

def test_a_duration_above_the_rooms_cap_refuses(rig):
    res = _offer(rig, duration="7d")     # room cap is 24h
    assert not res.ok and res.reason_code == "bad_duration"
    assert "24h" in res.text


def test_a_malformed_duration_refuses(rig):
    res = _offer(rig, duration="forever")
    assert not res.ok and res.reason_code == "bad_duration"


def test_a_missing_duration_refuses_rather_than_defaulting(rig):
    res = _offer(rig, duration="")
    assert not res.ok and res.reason_code == "bad_duration"


# --- check 4: the pause -----------------------------------------------------

def test_a_pause_on_the_social_scope_refuses(rig):
    from core import autonomy_control as ac
    # ⚠️ data_dir is REQUIRED: pausing without one pauses the developer's REAL
    # data home (a recorded landmine).
    ac.pause(rig.config.data_dir, scopes=("social",), set_by="test", via="test")
    res = _offer(rig)
    assert not res.ok and res.reason_code == "paused"


def test_an_unrelated_pause_scope_does_NOT_refuse(rig):
    """⚠️ `allows()` denies an UNKNOWN kind under every scope. Without a
    KIND_SCOPES row a `trading` pause would also stop a room action — a refusal
    the owner never asked for."""
    from core import autonomy_control as ac
    ac.pause(rig.config.data_dir, scopes=("trading",), set_by="t", via="t")
    assert _offer(rig).ok


# --- check 5: the target ----------------------------------------------------

def test_the_owner_is_never_targetable_by_their_surface_address(rig):
    """⚠️ With NO `group_roles` row — which is the production shape. Routing
    resolves the owner by principal and never writes them a row, so the roles
    table was the only thing standing between a member and a paid mute of the
    owner, and it is normally empty."""
    res = _offer(rig, target_user_id="5150")
    assert not res.ok and res.reason_code == "protected_target"


def test_an_unresolvable_owner_refuses_the_whole_sale(rig, monkeypatch):
    """⚠️ "I could not find the owner" and "the owner is not this person" are
    different facts, and only one of them is safe to act on."""
    monkeypatch.delenv("POLYROB_OWNER_TELEGRAM_ID", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    res = _offer(rig)
    assert not res.ok and res.reason_code == "owner_unknown"


def test_a_live_chat_administrator_is_protected_with_no_roles_row(rig):
    res = _offer(rig, target_status_fn=lambda *_a: "administrator")
    assert not res.ok and res.reason_code == "protected_target"


def test_an_unreadable_member_status_treats_the_target_as_PROTECTED(rig):
    def boom(*_a):
        raise RuntimeError("telegram down")
    res = _offer(rig, target_status_fn=boom)
    assert not res.ok and res.reason_code == "protected_target"


def test_an_ordinary_member_status_does_not_protect(rig):
    res = _offer(rig, target_status_fn=lambda *_a: "member")
    assert res.ok, res.text


def test_a_room_admin_is_never_targetable(rig):
    from core.surfaces.group_roles import GroupRoles
    GroupRoles(os.path.join(rig.config.data_dir, "surfaces.db")).grant(
        "telegram", "-100123", "9911", "admin", granted_by="u_owner")
    res = _offer(rig)
    assert not res.ok and res.reason_code == "protected_target"


def test_a_self_target_refuses_rather_than_selling_a_self_mute(rig):
    res = _offer(rig, target_user_id="2277")
    assert not res.ok and res.reason_code == "self_target"


def test_an_unreadable_owner_check_treats_the_target_as_PROTECTED(rig, monkeypatch):
    """⚠️ Fail-CLOSED. Never sell an action against someone who MIGHT be the
    owner."""
    def boom(surface, address):
        raise RuntimeError("identity store down")
    monkeypatch.setattr("core.surfaces.owner_target.is_owner_address", boom)
    res = _offer(rig)
    assert not res.ok and res.reason_code == "protected_target"


# --- check 6: the price band ------------------------------------------------

def test_a_verb_with_no_configured_price_refuses(rig):
    """`ban` is in the catalog and is NOT priced in this room."""
    res = _offer(rig, verb="ban", duration="1h")
    assert not res.ok and res.reason_code == "disabled"
    assert "price" in res.text


def test_an_agent_price_below_the_band_is_clamped_up_and_said_so(rig):
    res = _offer(rig, price_usd=0.01)             # band floor is 0.10
    assert res.ok, res.text
    assert "0.10" in res.text
    assert _store(rig).get(res.offer_id).price_usd == 0.10


def test_an_agent_price_above_the_band_is_clamped_down(rig):
    res = _offer(rig, price_usd=999.0)
    assert res.ok, res.text
    assert _store(rig).get(res.offer_id).price_usd == 5.0


def test_an_agent_price_inside_the_band_is_used_verbatim(rig):
    res = _offer(rig, price_usd=2.0)
    assert res.ok, res.text
    assert _store(rig).get(res.offer_id).price_usd == 2.0


# --- check 7: the caps ------------------------------------------------------

def test_the_payer_daily_cap_refuses(rig):
    from core.surfaces import chat_policy
    chat_policy.set(rig.config.data_dir, "u_owner", "telegram", "-100123",
                    "chat.paid_max_per_payer_day", 1)
    assert _offer(rig, target_user_id="1").ok
    res = _offer(rig, target_user_id="2")
    assert not res.ok and res.reason_code == "payer_cap"


def test_the_target_daily_cap_refuses(rig):
    from core.surfaces import chat_policy
    chat_policy.set(rig.config.data_dir, "u_owner", "telegram", "-100123",
                    "chat.paid_max_per_target_day", 1)
    assert _offer(rig, requester_id="1").ok
    res = _offer(rig, requester_id="2")
    assert not res.ok and res.reason_code == "target_cap"


# --- check 8: the bot's rights ----------------------------------------------

def test_a_bot_without_the_right_refuses_BEFORE_any_offer_exists(rig):
    """⚠️ The never-sell-what-we-cannot-deliver check. It must run before the
    mint, or every offer in a room where the bot lost admin owes a credit."""
    res = _offer(rig, rights_fn=_rights())        # no rights at all
    assert not res.ok and res.reason_code == "missing_right"
    assert "can_restrict_members" in res.text
    assert _store(rig).open_offers("telegram", "-100123") == []


def test_a_raising_rights_probe_refuses(rig):
    def boom(surface, chat_id):
        raise RuntimeError("telegram down")
    res = _offer(rig, rights_fn=boom)
    assert not res.ok and res.reason_code == "missing_right"


# --- check 9: the price -----------------------------------------------------

def test_an_unpriceable_asset_refuses_and_never_mints_a_free_action(rig):
    res = _offer(rig, quoter=_quoter(verdict="WASH"))
    assert not res.ok and res.reason_code == "unpriceable"
    assert "WASH" in res.text
    assert _store(rig).open_offers("telegram", "-100123") == []


def test_a_pumped_quote_refuses_rather_than_selling_a_near_free_mute(rig):
    res = _offer(rig, quoter=_quoter(price=1000.0))
    assert not res.ok and res.reason_code == "unpriceable"
    assert "floor" in res.text


def test_no_quoter_at_all_refuses_for_a_volatile_asset(rig):
    res = _offer(rig, quoter=None)
    assert not res.ok and res.reason_code == "unpriceable"


def test_a_room_priced_in_an_unconfigured_asset_refuses(rig):
    from core.surfaces import chat_policy
    chat_policy.set(rig.config.data_dir, "u_owner", "telegram", "-100123",
                    "chat.paid_asset", "vanished")
    res = _offer(rig)
    assert not res.ok and res.reason_code == "unpriceable"
    assert "vanished" in res.text


# --- check 10: the mint -----------------------------------------------------

def test_a_failing_mint_refuses_and_writes_no_offer_row(rig):
    def boom(**kw):
        raise RuntimeError("store unavailable")
    res = _offer(rig, mint_fn=boom)
    assert not res.ok and res.reason_code == "mint_failed"
    assert _store(rig).open_offers("telegram", "-100123") == []


def test_no_minter_at_all_refuses(rig):
    res = _offer(rig, mint_fn=None)
    assert not res.ok and res.reason_code == "mint_failed"


# --- the structural guarantee ----------------------------------------------

def test_offer_takes_no_parameter_that_could_name_a_payable_address():
    """⚠️ 046 §6 T2/T3: a room line must have NO path to a payment address, and
    no way to choose the asset."""
    params = set(inspect.signature(ra.offer).parameters)
    for forbidden in ("recipient", "pay_to", "address", "treasury", "asset",
                      "asset_id", "chain"):
        assert forbidden not in params, (
            f"offer() accepts {forbidden!r} — a room line could then influence "
            f"where money goes or in what")


# --- 046 T8: the payment rail, checked BEFORE the mint -----------------------

def test_a_rail_that_cannot_settle_refuses_before_minting(rig):
    """⚠️ `offer()` checked only its own flag and the room. An instance with
    X402_INVOICE_ENABLED off — whose settlement watcher therefore never starts —
    would mint offers, take real money and settle nothing, forever."""
    minted = []
    res = _offer(rig, rail_check=lambda asset: "invoicing is off on this instance",
                 mint_fn=lambda **kw: minted.append(kw) or _mint(**kw))
    assert not res.ok and res.reason_code == "rail_unavailable"
    assert "invoicing is off" in res.text
    assert minted == [], "an offer was minted onto a rail that cannot settle it"
    assert not _store(rig).recent("telegram", "-100123")


def test_a_raising_rail_check_refuses_too(rig):
    def boom(asset):
        raise RuntimeError("probe exploded")
    res = _offer(rig, rail_check=boom)
    assert not res.ok and res.reason_code == "rail_unavailable"


def test_a_ready_rail_mints_normally(rig):
    res = _offer(rig, rail_check=lambda asset: None)
    assert res.ok, res.text


def test_the_offer_carries_its_invoice_so_a_seat_can_render_a_card(rig):
    """046 T12: core renders the TEXT; the seat turns the same facts into a
    picture. It cannot do that without the row."""
    res = _offer(rig)
    assert res.invoice and res.invoice.get("recipient")


# --- 046 T9: the rest of the catalog ----------------------------------------

def test_a_priced_ban_is_purchasable_with_a_duration(rig):
    """⚠️ `ban` was in the catalog, had a Telegram primitive, and had NO price
    field at all — so `/paid price ban` wrote a key nothing read and every
    attempt refused with "ban has no price"."""
    from core.surfaces import chat_policy
    for k, v in (("chat.paid_ban_usd", 2.0),
                 ("chat.paid_ban_max_duration", "2h")):
        ok, msg = chat_policy.set(rig.config.data_dir, "u_owner", "telegram",
                                  "-100123", k, v)
        assert ok, msg
    res = _offer(rig, verb="ban", duration="1h")
    assert res.ok, res.text
    assert _store(rig).get(res.offer_id).verb == "ban"

    over = _offer(rig, verb="ban", duration="3h")
    assert not over.ok and over.reason_code == "bad_duration"
    assert "2h" in over.text


def test_a_target_may_counter_pay_their_own_unmute(rig):
    """⚠️ The self-target guard refused exactly the shape counter-pay IS."""
    from core.surfaces import chat_policy
    ok, msg = chat_policy.set(rig.config.data_dir, "u_owner", "telegram",
                              "-100123", "chat.paid_unmute_usd", 0.25)
    assert ok, msg
    res = _offer(rig, verb="unmute", target_user_id="2277", duration="")
    assert res.ok, res.text


def test_a_self_target_is_still_refused_for_a_non_counter_pay_verb(rig):
    res = _offer(rig, target_user_id="2277")
    assert not res.ok and res.reason_code == "self_target"


def test_the_public_purpose_never_names_the_target(rig):
    """⚠️ `GET /api/x402/requests/{id}` is PUBLIC and serves `purpose` verbatim
    as the challenge description, so "mute Alice in telegram:-100…" published
    the name of the person an action was bought against."""
    seen = {}
    _offer(rig, mint_fn=lambda **kw: seen.update(kw) or _mint(**kw))
    assert "someone" not in seen["purpose"].lower()
    assert "9911" not in seen["purpose"]
    assert "mute" in seen["purpose"] and "-100123" in seen["purpose"]
