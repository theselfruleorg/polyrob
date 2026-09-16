"""046 Phase 1 invariants. A future change must break a TEST, not a room."""
import inspect


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



# --- the room session is UNCHANGED -----------------------------------------

def test_the_room_toolset_still_carries_no_money_tool():
    """⚠️ Phase 1 does NOT open Door B. The room session's power surface must be
    exactly what 044 left it."""
    from core.surfaces.room_policy import room_tool_ids
    from core.tool_capabilities import ids_with
    assert not (set(room_tool_ids()) & (ids_with("money") - {"web_fetch"}))
    assert "room_action" not in room_tool_ids()


def test_the_invoice_verbs_are_still_denied_in_a_room():
    from core.surfaces.room_policy import ROOM_DENIED_ACTIONS
    assert "x402_invoice_x402_request" in ROOM_DENIED_ACTIONS


# --- a room line can never name where money goes ---------------------------

def test_offer_takes_no_parameter_that_could_name_a_payable_address():
    """⚠️ 046 §6 T2/T3."""
    from core.surfaces.room_actions import offer
    params = set(inspect.signature(offer).parameters)
    for forbidden in ("recipient", "pay_to", "address", "treasury", "asset",
                      "asset_id", "chain"):
        assert forbidden not in params, (
            f"offer() accepts {forbidden!r} — a room line could then influence "
            f"where money goes, or in what")


def test_the_offer_text_is_rendered_by_core_not_by_a_caller():
    """Every payable value in the offer comes from the invoice row and the
    asset row, so a model-authored string can never become an instruction."""
    from core.surfaces.room_actions import render_offer
    src = inspect.getsource(render_offer)
    assert "invoice.get('recipient')" in src or 'invoice.get("recipient")' in src


# --- the catalog is CODE ----------------------------------------------------

def test_the_effect_catalog_is_not_reachable_from_configuration(monkeypatch):
    import importlib

    from core.surfaces import room_actions
    before = set(room_actions.VERBS)
    monkeypatch.setenv("ROOM_ACTION_VERBS", "mute,delete_everything")
    monkeypatch.setenv("ROOM_ACTION_EFFECTS", "mute,delete_everything")
    reloaded = importlib.reload(room_actions)
    try:
        assert set(reloaded.VERBS) == before
    finally:
        importlib.reload(room_actions)


def test_a_member_verb_needs_BOTH_the_closed_set_and_the_rooms_grant():
    """⚠️ Configuration alone can never make a new verb member-reachable.

    `/paid` joined the set as a READ: a member reaches this room's price list and
    nothing else, whatever `chat.member_verbs` says.
    """
    from core.surfaces.dispatcher import _MEMBER_GRANTABLE_COMMANDS
    assert _MEMBER_GRANTABLE_COMMANDS == frozenset(
        {"/mute", "/unmute", "/ban", "/unban", "/help", "/paid"})


def test_a_members_paid_reaches_exactly_one_read():
    """⚠️ The member lane must never widen to a configuration verb. `status`
    names the caps, the asset, the open offers and the owner's remedies."""
    from surfaces.telegram.group_ops import (_PAID_MEMBER_VERBS,
                                             _PAID_OWNER_ONLY_VERBS,
                                             _PAID_OWNER_OR_ADMIN_VERBS)
    assert _PAID_MEMBER_VERBS == frozenset({"prices"})
    assert not (_PAID_MEMBER_VERBS
                & (_PAID_OWNER_ONLY_VERBS | _PAID_OWNER_OR_ADMIN_VERBS))


# --- telling the model is a DESCRIPTION, not a capability -------------------

def test_the_room_turn_is_told_about_the_rail_and_still_cannot_reach_it():
    """046: the model is told paid verbs exist HERE — and the room session keeps
    the 044 read-only toolset with the invoice verb denied. A description that
    came with a capability would be the whole point of 044 undone."""
    import inspect

    from core.surfaces import room_actions
    from core.surfaces.room_policy import DEFAULT_ROOM_TOOLS, ROOM_DENIED_ACTIONS
    src = inspect.getsource(room_actions.describe_for_model)
    assert "do NOT perform" in src
    assert "x402_invoice_x402_request" in ROOM_DENIED_ACTIONS
    assert "x402_invoice" not in DEFAULT_ROOM_TOOLS


def test_the_surface_block_renders_the_rooms_own_note():
    import inspect

    from agents.task.agent import prompts
    from core.surfaces import binding
    assert "chat_paid_actions" in inspect.getsource(
        prompts.SystemPrompt._get_surface_content)
    assert "describe_for_model" in inspect.getsource(binding._room_paid_note)


# --- the pause asymmetry ----------------------------------------------------

def test_apply_is_not_pause_gated_and_offer_is():
    """⚠️ We already hold the payer's money at apply time. Stranding the effect
    behind a pause is a silent default on an obligation."""
    from core.surfaces import room_actions
    assert "allows(" in inspect.getsource(room_actions.offer)
    assert "allows(" not in inspect.getsource(room_actions.apply)


def test_room_action_offer_has_a_pause_kind_row():
    """⚠️ `allows()` denies an UNKNOWN kind under EVERY scope, so without a row
    a `trading` pause would also stop a room action."""
    from core.autonomy_control import KIND_SCOPES
    assert KIND_SCOPES.get("room_action_offer") == ("all", "social")


# --- order of operations ----------------------------------------------------

def test_the_rights_check_runs_before_the_mint():
    """⚠️ Never sell what we cannot deliver. If the probe moved after the mint,
    every offer in a room where the bot lost admin would owe a credit."""
    from core.surfaces.room_actions import offer
    src = inspect.getsource(offer)
    assert src.index("telegram_right") < src.index("mint_fn("), (
        "the bot-rights probe moved AFTER the mint")


def test_the_quote_runs_before_the_mint_too():
    """An unpriceable action must never produce an invoice."""
    from core.surfaces.room_actions import offer
    src = inspect.getsource(offer)
    assert src.index("size_amount_raw(") < src.index("mint_fn(")


# --- no refund path ---------------------------------------------------------

def test_the_failure_path_pays_a_credit_and_the_text_promises_no_refund():
    """⚠️ An automatic outbound refund is a money-SPEND verb: a tx_guard intent,
    the owner-approved lane and every money gate list. The credit discharges the
    obligation with no new spend path, and the payer-facing TEXT must never
    suggest otherwise."""
    import time

    from core.surfaces import room_actions
    from core.surfaces.room_action_store import Offer

    class _Store:
        def set_status(self, *a, **kw):
            return True

    row = Offer(offer_id="o", surface="telegram", chat_id="-1", verb="mute",
                target_user_id="9", target_name="S", duration_sec=60,
                price_usd=1.0, asset_id="rob", amount_raw="1",
                requester_id="2", invoice_id="i", status="paid", reason="",
                created_at=time.time())
    res = room_actions._credit(None, _Store(), row, "bot lost admin", 1.0)
    assert not res.ok
    assert "credit" in res.text.lower()
    assert "refund" not in res.text.lower()


# --- every list a chat verb needs ------------------------------------------

def test_paid_is_in_every_list_a_chat_verb_needs():
    """⚠️ FIVE lists. Miss one and the handler is dead."""
    from core.surfaces.dispatcher import _COMMANDS, _GROUP_ADMIN_COMMANDS
    from surfaces.telegram.harness import (_HELP_BODY, _OWNER_ADMIN_COMMANDS,
                                           _ROOM_REFUSED_COMMANDS)
    assert "/paid" in _COMMANDS                     # routable at all
    assert "/paid" in _GROUP_ADMIN_COMMANDS         # a room admin can reach it
    assert "/paid" in _OWNER_ADMIN_COMMANDS         # dispatched as owner-admin
    assert "/paid" in _HELP_BODY                    # discoverable
    assert "/paid" not in _ROOM_REFUSED_COMMANDS    # deliberately room-scoped


# --- the flag ---------------------------------------------------------------

def test_every_flag_has_a_catalog_row():
    from core.flags_catalog import CATALOG
    assert "ROOM_ACTIONS_ENABLED" in {row[0] for row in CATALOG}


def test_the_feature_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ROOM_ACTIONS_ENABLED", raising=False)
    from core.surfaces.room_actions import room_actions_enabled
    assert room_actions_enabled() is False


def test_a_room_is_paid_disabled_by_default():
    from core.surfaces.chat_policy import ChatPolicy
    p = ChatPolicy.defaults()
    assert p.paid_enabled is False
    assert p.member_verbs == ("help",)


# --- 046 phase 2 invariants -------------------------------------------------

def test_every_catalog_verb_has_a_price_band_branch():
    """⚠️ The catalog advertised five verbs and priced two. A refusal echoed
    `ban, unban, slowmode` as things "I sell", and every attempt to buy one was
    refused with "has no price" — including after an owner ran `/paid price
    ban`, which wrote a key nothing read."""
    from core.surfaces.chat_policy import ChatPolicy
    from core.surfaces.room_actions import VERBS, _price_band
    policy = ChatPolicy()
    for verb in VERBS:
        band = _price_band(policy, verb)
        assert band == (0.0, 0.0, 0.0), f"{verb}: unpriced must be a zero band"
    priced = ChatPolicy(paid_mute_usd=1.0, paid_ban_usd=2.0,
                        paid_unmute_usd=0.5, paid_unban_usd=0.75)
    for verb in VERBS:
        assert _price_band(priced, verb)[0] > 0, (
            f"{verb} has no branch in _price_band — an owner can set its key "
            f"and nothing reads it")


def test_the_admin_seat_reads_prices_through_the_ONE_band_function():
    """A second, dynamic reader returned the DEFAULT for a verb whose field does
    not exist, so a verb read as priced in one place and unpriced in another."""
    import inspect

    from core.surfaces import room_action_admin
    # Comment lines are stripped: the point is that no CODE line does the
    # dynamic read (the module's own comment explains why it must not).
    src = "\n".join(line for line in
                    inspect.getsource(room_action_admin).splitlines()
                    if not line.strip().startswith("#"))
    assert 'f"paid_{verb}_usd"' not in src
    assert "_price_band" in src


def test_the_owner_check_is_not_the_permanently_false_one():
    """⚠️ `core.instance.is_owner(uid)` with no principal is `bool("") and …` —
    a PERMANENT False. It was the whole owner protection."""
    import inspect

    from core.surfaces import room_actions
    src = "\n".join(line for line in
                    inspect.getsource(room_actions._target_protection).splitlines()
                    if not line.strip().startswith("#"))
    assert "is_owner_address" in src
    assert "core.instance import is_owner" not in src


def test_apply_resolves_the_moderator_from_the_container():
    import inspect

    from core.surfaces import room_actions
    src = inspect.getsource(room_actions.apply)
    assert 'get_service("room_moderator")' in src


def test_the_credit_path_has_a_redeemer():
    import inspect

    from core.surfaces import room_actions
    assert "redeemable_credit" in inspect.getsource(room_actions._redeem_credit)
    assert "_redeem_credit" in inspect.getsource(room_actions.offer)


def test_mark_settled_only_moves_a_PENDING_offer():
    import inspect

    from core.surfaces import room_actions
    assert "settle_pending" in inspect.getsource(room_actions.mark_settled)


def test_the_room_reply_can_name_its_own_destination():
    """046 T2: a member's paid offer is a quote addressed to the member who
    asked. The 044 owner-only redirect sent it to the OWNER's DM."""
    import inspect

    from surfaces.telegram import group_ops
    src = inspect.getsource(group_ops._paid_offer)
    assert "to_room=True" in src
