"""The first Telegram moderation calls in the tree. Typed results, never raises."""
import pytest

from surfaces.telegram import moderation as mod


class _Member:
    def __init__(self, **kw):
        self.status = kw.pop("status", "administrator")
        for k, v in kw.items():
            setattr(self, k, v)


class _Bot:
    def __init__(self, member=None, fail=None, call_fail=None):
        self._member = member
        self._fail = fail
        self._call_fail = call_fail
        self.calls = []

    async def get_me(self):
        return type("Me", (), {"id": 4242})()

    async def get_chat_member(self, chat_id, user_id):
        if self._fail:
            raise self._fail
        return self._member

    async def restrict_chat_member(self, **kw):
        if self._call_fail:
            raise self._call_fail
        self.calls.append(("restrict", kw))

    async def ban_chat_member(self, **kw):
        self.calls.append(("ban", kw))

    async def unban_chat_member(self, **kw):
        self.calls.append(("unban", kw))



@pytest.mark.asyncio
async def test_bot_rights_reads_the_permission_flags_it_actually_holds():
    bot = _Bot(_Member(can_restrict_members=True, can_change_info=False))
    rights = await mod.bot_rights(bot, "-100123")
    assert "can_restrict_members" in rights
    assert "can_change_info" not in rights


@pytest.mark.asyncio
async def test_a_plain_member_bot_holds_no_rights():
    assert await mod.bot_rights(_Bot(_Member(status="member")), "-1") == set()


@pytest.mark.asyncio
async def test_an_api_failure_reads_as_NO_rights_never_as_all_of_them():
    """⚠️ Fail-CLOSED. An unreadable permission set must refuse the sale, not
    assume it."""
    bot = _Bot(fail=RuntimeError("telegram down"))
    assert await mod.bot_rights(bot, "-100123") == set()


@pytest.mark.asyncio
async def test_a_creator_bot_holds_its_granted_rights():
    bot = _Bot(_Member(status="creator", can_restrict_members=True))
    assert "can_restrict_members" in await mod.bot_rights(bot, "-1")


@pytest.mark.asyncio
async def test_restrict_sends_an_all_false_permission_set_with_an_until_date():
    import time
    bot = _Bot()
    # Far enough ahead that the min-offset clamp does not move it — a PAST
    # timestamp would be clamped up, which is the next test's subject.
    until = int(time.time()) + 3600
    res = await mod.restrict_member(bot, "-100123", "9911", until_ts=until)
    assert res.ok
    op, kw = bot.calls[0]
    assert op == "restrict"
    assert kw["chat_id"] == "-100123" and kw["user_id"] == 9911
    assert kw["until_date"] == until
    perms = kw["permissions"]
    assert getattr(perms, "can_send_messages") is False
    assert getattr(perms, "can_send_other_messages") is False


def test_an_until_date_too_close_to_now_is_clamped_up():
    """⚠️ Telegram reads an until_date under 30s from now as FOREVER. A
    forever-mute sold as a minute is exactly the quiet overreach this rail must
    not have."""
    assert mod.clamp_until(1000, now=1000.0) == 1031
    assert mod.clamp_until(9_000, now=1000.0) == 9_000


@pytest.mark.asyncio
async def test_a_telegram_error_is_a_typed_failure_not_an_exception():
    """⚠️ The caller owes a credit on failure, and can only do that if it is
    told instead of being unwound."""
    bot = _Bot(call_fail=RuntimeError("CHAT_ADMIN_REQUIRED"))
    res = await mod.restrict_member(bot, "-100123", "9911", until_ts=9_000_000_000)
    assert res.ok is False
    assert "CHAT_ADMIN_REQUIRED" in res.reason


@pytest.mark.asyncio
async def test_unrestrict_restores_the_default_send_permissions():
    bot = _Bot()
    res = await mod.unrestrict_member(bot, "-100123", "9911")
    assert res.ok
    perms = bot.calls[0][1]["permissions"]
    assert getattr(perms, "can_send_messages") is True
    assert bot.calls[0][1]["until_date"] == 0


@pytest.mark.asyncio
async def test_ban_and_unban_reach_their_own_api_calls():
    bot = _Bot()
    assert (await mod.ban_member(bot, "-1", "9", until_ts=9_000_000_000)).ok
    assert (await mod.unban_member(bot, "-1", "9")).ok
    assert [c[0] for c in bot.calls] == ["ban", "unban"]
    assert bot.calls[1][1]["only_if_banned"] is True


def test_every_catalog_verb_maps_to_a_method_the_client_actually_has():
    """⚠️ `slowmode` shipped in the catalog with no Bot API method behind it —
    `Bot.set_chat_slow_mode_delay` does not exist on aiogram — so every
    slowmode sale would have taken money and written a credit. A verb we cannot
    perform must not be in the vocabulary a refusal echoes as "I sell"."""
    from aiogram import Bot

    from core.surfaces.room_actions import VERBS
    needed = {"mute": "restrict_chat_member", "unmute": "restrict_chat_member",
              "ban": "ban_chat_member", "unban": "unban_chat_member"}
    assert set(VERBS) == set(needed), (
        "a catalog verb has no entry here — name the API method it needs")
    for verb, method in needed.items():
        assert hasattr(Bot, method), f"{verb} needs Bot.{method}"


def test_unmute_restores_every_permission_the_api_knows():
    """⚠️ `_UNMUTED` was `{k: True for k in _MUTED}`, and `restrictChatMember`
    reads an OMITTED flag as False — so every "unmute" silently STRIPPED
    can_invite_users, can_pin_messages, can_change_info, can_manage_topics,
    can_react_to_messages and can_edit_tag."""
    from aiogram.types import ChatPermissions
    assert set(mod._UNMUTED) == set(ChatPermissions.model_fields)
    assert all(v is True for v in mod._UNMUTED.values())


@pytest.mark.asyncio
async def test_no_moderation_call_raises_on_a_bot_that_lacks_the_method():
    class _Bare:
        pass
    res = await mod.ban_member(_Bare(), "-1", "9", until_ts=9_000_000_000)
    assert res.ok is False and res.reason
