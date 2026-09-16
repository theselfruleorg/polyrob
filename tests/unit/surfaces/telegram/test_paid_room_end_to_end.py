"""046 phase 2: the WHOLE rail, with the real wiring, in one test.

⚠️ This is the test the feature did not have. Every phase-1 test injected
`perform_fn`/`mint_fn`/`rights_fn`, so 157 of them passed while production had no
adapter, no container on the watcher, and a reply that went to the owner. This
one registers the REAL moderator on a container, mints through the REAL
`create_payment_request`, settles through the REAL store transition, and applies
through the REAL service lookup. Only the network (aiogram, the chain) is faked.
"""
import os

import pytest

from core.surfaces import room_actions as ra
from core.surfaces.command_reply import reply_media, reply_text, reply_to_room
from surfaces.telegram import group_ops
from surfaces.telegram.room_moderator import install_room_moderator


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



class _Bot:
    """A Telegram that answers, and records."""

    def __init__(self, bot_id=4242, right=True):
        self.calls = []
        self._id = bot_id
        self._right = right

    async def get_me(self):
        return type("Me", (), {"id": self._id})()

    async def get_chat_member(self, chat_id, user_id):
        if int(user_id) == self._id:
            return type("M", (), {"status": "administrator",
                                  "can_restrict_members": self._right,
                                  "can_change_info": False,
                                  "can_delete_messages": False,
                                  "can_pin_messages": False})()
        return type("M", (), {"status": "member"})()

    async def restrict_chat_member(self, **kw):
        self.calls.append(("restrict", kw))

    async def ban_chat_member(self, **kw):
        self.calls.append(("ban", kw))


class _Router:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram",
                           media=None):
        self.sent.append((surface_id, chat_id, text))
        return True


class _Container:
    def __init__(self, data_dir, router):
        self.config = type("Cfg", (), {"data_dir": str(data_dir)})()
        self._services = {"message_router": router}

    def get_service(self, name):
        return self._services.get(name)

    def register_service(self, name, value):
        self._services[name] = value


class _Agent:
    def __init__(self, container, bot):
        self.container = container
        self.bot = bot
        self.surface = type("S", (), {"_bot": bot})()


def _result(*, text, reply_from, user_id="2277", chat_id="-100123"):
    msg = {"chat": {"id": chat_id, "type": "supergroup"}, "text": text,
           "reply_to_message": {"message_id": 7, "from": reply_from}}
    src = type("Src", (), {"surface_id": "telegram", "chat_id": chat_id,
                           "chat_type": "supergroup"})()
    identity = type("Id", (), {"user_id": user_id, "raw_user_id": user_id,
                               "source": src, "chat_role": "member"})()
    inbound = type("In", (), {"text": text, "identity": identity,
                              "raw": {"message": msg}})()
    return type("Res", (), {"inbound": inbound})()


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("ROOM_ACTION_CARD_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "5150")
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.delenv("PAYMENT_DEFAULT_ASSET", raising=False)
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: "u_owner")

    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-100123")
    from core.surfaces import chat_policy
    for k, v in (("chat.paid_enabled", True), ("chat.paid_mute_usd", 0.50),
                 ("chat.member_verbs", ["help", "mute"])):
        ok, msg = chat_policy.set(str(tmp_path), "u_owner", "telegram",
                                  "-100123", k, v)
        assert ok, msg

    router = _Router()
    container = _Container(tmp_path, router)
    bot = _Bot()
    assert install_room_moderator(container, type("S", (), {"_bot": bot})())
    agent = _Agent(container, bot)

    minted = {}

    def fake_mint(**kw):
        minted.update(kw)
        return {"request_id": "inv_1", "recipient": "0x" + "11" * 20,
                "amount_raw": str(kw["amount_raw"]), "amount_usd": kw["price_usd"],
                "asset": "usdc", "chain": kw["asset"].chain,
                "purpose": kw["purpose"], "expires_at_epoch": 4_102_444_800}

    monkeypatch.setattr(group_ops, "_mint_fn", lambda a: fake_mint)
    return agent, bot, router, minted, str(tmp_path)


def test_a_member_buys_a_mute_and_the_whole_rail_runs(rig):
    agent, bot, router, minted, home = rig

    # 1. the member asks, in reply to the person they want muted
    out = mute_reply(
        agent, _result(text="/mute 30m",
                       reply_from={"id": 9911, "first_name": "Sam"}), ["30m"])

    # 2. the offer goes to the ROOM, with a payment card
    assert reply_to_room(out), "the payer would never have seen this"
    text = reply_text(out)
    assert "💰" in text and "0.50" in text and "0x" in text
    media = reply_media(out)
    assert media and os.path.isfile(media[0]["path"])

    # 3. ...and it never named the target in the PUBLIC purpose
    assert "Sam" not in minted["purpose"]

    # 4. the offer row is pending, and the rights probe ran against the bot
    from core.surfaces.room_action_store import OfferStore, store_path
    store = OfferStore(store_path(home))
    rows = store.recent("telegram", "-100123")
    assert len(rows) == 1 and rows[0].status == "pending"
    offer_id = rows[0].offer_id

    # 5. the payment lands: the store transition, then the effect
    import asyncio
    assert ra.mark_settled(agent.container, offer_id)
    res = asyncio.run(ra.apply(agent.container, offer_id))
    assert res.ok, res.text
    assert store.get(offer_id).status == "applied"

    # 6. Telegram was actually called, for the right person, with a real deadline
    assert [c[0] for c in bot.calls] == ["restrict"]
    kw = bot.calls[0][1]
    assert kw["user_id"] == 9911 and kw["until_date"] > 0
    assert kw["permissions"].can_send_messages is False


def test_the_bot_losing_its_right_refuses_before_any_money_is_asked_for(rig):
    agent, bot, router, minted, home = rig
    bot._right = False
    out = mute_reply(
        agent, _result(text="/mute 30m",
                       reply_from={"id": 9911, "first_name": "Sam"}), ["30m"])
    assert "can_restrict_members" in reply_text(out)
    assert not reply_to_room(out)
    from core.surfaces.room_action_store import OfferStore, store_path
    assert not OfferStore(store_path(home)).recent("telegram", "-100123")
    assert minted == {}


def test_the_owner_cannot_be_bought_a_mute_of(rig):
    agent, bot, router, minted, home = rig
    out = mute_reply(
        agent, _result(text="/mute 30m",
                       reply_from={"id": 5150, "first_name": "Owner"}), ["30m"])
    assert "cannot be targeted" in reply_text(out)
    assert minted == {}


def test_a_failed_effect_pays_a_credit_that_the_next_ask_spends(rig, monkeypatch):
    agent, bot, router, minted, home = rig
    out = mute_reply(
        agent, _result(text="/mute 30m",
                       reply_from={"id": 9911, "first_name": "Sam"}), ["30m"])
    assert reply_to_room(out)

    from core.surfaces.room_action_store import OfferStore, store_path
    store = OfferStore(store_path(home))
    offer_id = store.recent("telegram", "-100123")[0].offer_id

    async def boom(**kw):
        raise RuntimeError("CHAT_ADMIN_REQUIRED")
    monkeypatch.setattr(bot, "restrict_chat_member", boom)

    import asyncio
    assert ra.mark_settled(agent.container, offer_id)
    res = asyncio.run(ra.apply(agent.container, offer_id))
    assert not res.ok and "credit" in res.text
    assert store.get(offer_id).status == "credited"

    # the SAME member asks again: the credit is spent, no invoice is minted
    monkeypatch.setattr(bot, "restrict_chat_member", _Bot.restrict_chat_member.__get__(bot))
    minted.clear()
    again = mute_reply(
        agent, _result(text="/mute 30m",
                       reply_from={"id": 9911, "first_name": "Sam"}), ["30m"])
    assert "credit" in reply_text(again)
    assert minted == {}, "a redeemed credit minted an invoice"
    assert store.get(offer_id).status == "redeemed"


def test_the_settlement_watcher_closes_the_loop_into_the_room(rig):
    """The last leg, through the REAL watcher branch: a settled `room_action`
    invoice actuates the effect and the ROOM is told.

    ⚠️ This is the leg that had no writer at all — `_room_container` was set by
    nobody, so `apply` reached no transport and every settled action was
    credited; and the receipt was a `logger.info`, so nobody in the room ever
    learned it landed.
    """
    import asyncio

    agent, bot, router, minted, home = rig
    mute_reply(
        agent, _result(text="/mute 30m",
                       reply_from={"id": 9911, "first_name": "Sam"}), ["30m"])
    from core.surfaces.room_action_store import OfferStore, store_path
    store = OfferStore(store_path(home))
    offer_id = store.recent("telegram", "-100123")[0].offer_id

    from modules.x402.settlement_notify import SettlementNotifyMixin
    notices = []

    class _W(SettlementNotifyMixin):
        _db = None
        _room_moderator = None

        def __init__(self, container):
            self._room_container = container
            self.task_agent = agent

        async def _deliver_session_notice(self, inv, text, kind):
            return False

        async def _push_owner_notice(self, user_id, text, **kw):
            notices.append(text)

    asyncio.run(_W(agent.container)._notify({
        "request_id": "inv_1", "kind": "room_action",
        "room_action": {"offer_id": offer_id}, "amount_usd": 0.5,
        "user_id": "u_owner", "session_id": ""}))

    assert store.get(offer_id).status == "applied"
    assert [c[0] for c in bot.calls] == ["restrict"]
    assert router.sent, "the room was never told"
    surface_id, chat_id, text = router.sent[0]
    assert surface_id == "telegram" and chat_id == "-100123"
    assert "Sam is muted" in text
    assert notices == [], "a success must not page the owner"
