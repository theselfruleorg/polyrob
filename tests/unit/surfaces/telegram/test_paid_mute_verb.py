"""Reply-/mute is the paid member action; bare /mute is unchanged (046)."""
import pytest

from core.surfaces.command_reply import reply_text, reply_to_room
from surfaces.telegram import group_ops


# --- async-seam shim (2026-09-15) -------------------------------------------
# The Telegram verb handlers became `async def` so a bot read runs on the
# CALLER's event loop instead of being bridged to another one — the "got Future
# attached to a different loop" outage that made every target read as PROTECTED
# and the owner's free /ban do nothing. Driven synchronously here, exactly as
# this suite already wraps `groups_reply`.
import asyncio as _aio
from surfaces.telegram import group_ops as _gops


def _sync_seam(fn):
    def _call(*a, **k):
        r = fn(*a, **k)
        return _aio.run(r) if _aio.iscoroutine(r) else r
    return _call

mute_reply = _sync_seam(_gops.mute_reply)
unmute_reply = _sync_seam(_gops.unmute_reply)
ban_reply = _sync_seam(_gops.ban_reply)



def _result(*, text, reply_from=None, user_id="2277", chat_id="-100123",
            chat_type="supergroup", chat_role="member"):
    msg = {"chat": {"id": chat_id, "type": chat_type}, "text": text}
    if reply_from:
        msg["reply_to_message"] = {"message_id": 7, "from": reply_from}
    src = type("Src", (), {"surface_id": "telegram", "chat_id": chat_id,
                           "chat_type": chat_type})()
    identity = type("Id", (), {"user_id": user_id, "raw_user_id": user_id,
                               "source": src, "chat_role": chat_role})()
    inbound = type("In", (), {"text": text, "identity": identity,
                              "raw": {"message": msg}})()
    return type("Res", (), {"inbound": inbound})()


class _FakeAgent:
    def __init__(self, tmp_path):
        self.container = type("C", (), {
            "config": type("Cfg", (), {"data_dir": str(tmp_path)})(),
            "get_service": lambda self, n: None})()


# --- the split --------------------------------------------------------------

def test_reply_target_reads_the_replied_to_sender():
    res = _result(text="/mute 1h",
                  reply_from={"id": 9911, "first_name": "Someone"})
    assert group_ops._reply_target(res) == ("9911", "Someone", False)


def test_reply_target_falls_back_to_the_username_then_the_id():
    assert group_ops._reply_target(
        _result(text="/mute", reply_from={"id": 9911, "username": "sam"})
    ) == ("9911", "sam", False)
    assert group_ops._reply_target(
        _result(text="/mute", reply_from={"id": 9911})) == ("9911", "9911", False)


def test_no_reply_means_no_target():
    assert group_ops._reply_target(_result(text="/mute here 2h")) is None


def test_a_bare_mute_from_an_admin_still_silences_the_ROOM(monkeypatch, tmp_path):
    """⚠️ Regression guard for the pre-046 verb. It must not change."""
    called = {}

    def _room_mute(*a, **kw):
        called["room"] = a
        return "🔇 ok"

    monkeypatch.setattr(group_ops.group_admin, "mute", _room_mute)
    out = mute_reply(_FakeAgent(tmp_path),
                               _result(text="/mute here 2h", chat_role="admin"),
                               ["here", "2h"])
    assert "🔇" in out
    assert "room" in called


def test_a_bare_mute_from_a_member_is_still_owner_or_admin_only(tmp_path):
    out = mute_reply(_FakeAgent(tmp_path),
                               _result(text="/mute here 2h"), ["here", "2h"])
    assert "🔒" in out


# --- the paid member action -------------------------------------------------

def test_a_member_reply_mute_mints_a_paid_offer(monkeypatch, tmp_path):
    seen = {}

    def fake_offer(container, **kw):
        seen.update(kw)
        from core.surfaces.room_actions import OfferResult
        return OfferResult(True, "💰 offer text", offer_id="off_1")

    monkeypatch.setattr("core.surfaces.room_actions.offer", fake_offer)
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: True)
    out = mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute 1h", reply_from={"id": 9911, "first_name": "S"}),
        ["1h"])
    assert "💰" in reply_text(out)
    # ⚠️ The offer is delivered IN THE ROOM. It used to take the 044 owner-only
    # redirect, so the quote — price, address, offer id — went to the OWNER's DM
    # and the member who asked saw silence.
    assert reply_to_room(out)
    assert seen["verb"] == "mute"
    assert seen["target_user_id"] == "9911"
    assert seen["requester_id"] == "2277"
    assert seen["duration"] == "1h"
    assert seen["chat_id"] == "-100123"
    assert callable(seen["rights_fn"]) and callable(seen["mint_fn"])


def test_a_member_reply_mute_with_no_duration_defaults_to_an_hour(
        monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("core.surfaces.room_actions.offer",
                        lambda c, **kw: seen.update(kw) or _ok())
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: True)
    mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute", reply_from={"id": 9911, "first_name": "S"}), [])
    assert seen["duration"] == "1h"


def test_a_member_whose_room_did_not_grant_the_verb_is_refused(
        monkeypatch, tmp_path):
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: False)
    out = mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute 1h", reply_from={"id": 9911, "first_name": "S"}),
        ["1h"])
    assert "member verb" in reply_text(out)
    assert "member_verbs" in reply_text(out)
    # ⚠️ A refusal stays owner-only output: it can name a protection or a cap.
    assert not reply_to_room(out)


def test_an_owner_reply_mute_applies_FREE_and_never_mints_an_invoice(
        monkeypatch, tmp_path):
    """⚠️ An owner already holds the authority. Charging for it is theatre."""
    minted, applied = [], []
    monkeypatch.setattr("core.surfaces.room_actions.offer",
                        lambda c, **kw: minted.append(kw) or _ok())
    monkeypatch.setattr(group_ops, "_apply_free",
                        lambda *a, **kw: applied.append(a) or "muted")
    out = mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute 1h", chat_role="owner",
                reply_from={"id": 9911, "first_name": "S"}),
        ["1h"])
    assert minted == []
    assert applied and "muted" in reply_text(out)


def test_an_admin_reply_mute_also_applies_free(monkeypatch, tmp_path):
    applied = []
    monkeypatch.setattr(group_ops, "_apply_free",
                        lambda *a, **kw: applied.append(a) or "muted")
    mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute 1h", chat_role="admin",
                reply_from={"id": 9911, "first_name": "S"}),
        ["1h"])
    assert applied


def test_a_reply_mute_in_a_DM_is_refused(tmp_path):
    """⚠️ `chat_type` here is the NORMALIZED value `dm`, not Telegram's raw
    `private` — `_is_dm` compares against `dm`, so a test using the raw value
    would pass down the wrong branch and prove nothing."""
    out = mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute 1h", chat_type="dm",
                reply_from={"id": 9911, "first_name": "S"}),
        ["1h"])
    assert "in reply only works inside a group" in reply_text(out)


# --- the member-verb grant --------------------------------------------------

def test_member_may_use_reads_the_rooms_own_grant(tmp_path, monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")
    from core.surfaces import chat_policy
    agent = _FakeAgent(tmp_path)
    assert not group_ops._member_may_use(agent.container, "telegram",
                                         "-100123", "mute")
    ok, msg = chat_policy.set(str(tmp_path), "u_owner", "telegram", "-100123",
                              "chat.member_verbs", ["help", "mute"])
    assert ok, msg
    assert group_ops._member_may_use(agent.container, "telegram", "-100123",
                                     "mute")


def test_member_may_use_fails_closed_on_an_unreadable_policy(tmp_path):
    """An unreadable policy grants NOTHING."""
    class _Broken:
        config = None

        def get_service(self, n):
            raise RuntimeError("down")
    assert not group_ops._member_may_use(_Broken(), "telegram", "-1", "mute")


def _ok():
    from core.surfaces.room_actions import OfferResult
    return OfferResult(True, "💰 ok", offer_id="off_1")


# --- 046 phase 2: the rest of the catalog -----------------------------------

def test_a_member_reply_ban_mints_a_paid_offer_for_ban(monkeypatch, tmp_path):
    """⚠️ `ban` was in the catalog, had a Telegram primitive, and was
    unpurchasable: no price field, no seat, and the free path called
    `restrict_member` whatever verb it was handed."""
    seen = {}
    monkeypatch.setattr("core.surfaces.room_actions.offer",
                        lambda c, **kw: seen.update(kw) or _ok())
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: True)
    ban_reply(
        _FakeAgent(tmp_path),
        _result(text="/ban 2h", reply_from={"id": 9911, "first_name": "S"}),
        ["2h"])
    assert seen["verb"] == "ban" and seen["duration"] == "2h"


def test_a_target_may_counter_pay_their_own_unmute(monkeypatch, tmp_path):
    """⚠️ The counter-pay the owner asked for. `unmute` was priced in the schema
    and unreachable: no seat, and the self-target guard refused exactly the
    shape it is FOR.

    ⚠️ On Telegram the line is usually typed by somebody ELSE — a muted member
    cannot send anything, including `/unmute`. The rule is still right (it is
    what lets a friend pay to lift it, and what a surface where a silenced
    member can still issue a command would need), but the guide must not sell it
    as "buy your own way out" without saying who can type it."""
    seen = {}
    monkeypatch.setattr("core.surfaces.room_actions.offer",
                        lambda c, **kw: seen.update(kw) or _ok())
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: True)
    unmute_reply(
        _FakeAgent(tmp_path),
        _result(text="/unmute", user_id="9911",
                reply_from={"id": 9911, "first_name": "S"}), [])
    assert seen["verb"] == "unmute"
    assert seen["target_user_id"] == seen["requester_id"] == "9911"


def test_replying_to_the_BOT_is_refused(monkeypatch, tmp_path):
    """⚠️ A member can reply to the agent's own message. A paid action there
    would be the room buying the agent's silence."""
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: True)
    out = mute_reply(
        _FakeAgent(tmp_path),
        _result(text="/mute 1h",
                reply_from={"id": 4242, "first_name": "Rob", "is_bot": True}),
        ["1h"])
    assert "cannot be targeted" in reply_text(out)


def test_a_bare_ban_has_no_room_meaning_and_says_so(tmp_path):
    out = ban_reply(_FakeAgent(tmp_path),
                              _result(text="/ban 2h", chat_role="admin"), ["2h"])
    assert "names a person" in reply_text(out)


def test_a_third_party_may_also_pay_to_unmute_someone(monkeypatch, tmp_path):
    """The reachable half of counter-pay on Telegram."""
    seen = {}
    monkeypatch.setattr("core.surfaces.room_actions.offer",
                        lambda c, **kw: seen.update(kw) or _ok())
    monkeypatch.setattr(group_ops, "_member_may_use", lambda *a, **kw: True)
    unmute_reply(
        _FakeAgent(tmp_path),
        _result(text="/unmute", user_id="2277",
                reply_from={"id": 9911, "first_name": "S"}), [])
    assert seen["verb"] == "unmute"
    assert seen["target_user_id"] == "9911" and seen["requester_id"] == "2277"
