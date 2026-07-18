"""Wave 3 Task 4 — pure MESSAGE_CREATE → InboundMessage conversion."""
from surfaces.discord.gateway import parse_message_create

BOT_ID = "999"


def _payload(**kw):
    d = {
        "id": "msg-1",
        "channel_id": "chan-7",
        "content": "hello there",
        "author": {"id": "42", "username": "alice", "bot": False},
        "mentions": [],
    }
    d.update(kw)
    return d


def test_basic_dm_parse():
    inbound = parse_message_create(_payload(), BOT_ID)
    assert inbound is not None
    assert inbound.text == "hello there"
    assert inbound.identity.source.surface_id == "discord"
    assert inbound.identity.source.chat_id == "chan-7"
    assert inbound.identity.source.chat_type == "dm"
    assert inbound.identity.raw_user_id == "42"
    assert inbound.identity.user_id == "u_discord_42"
    assert inbound.idempotency_key == "msg-1"
    assert inbound.mentions_bot is False


def test_guild_message_is_group():
    inbound = parse_message_create(_payload(guild_id="g1"), BOT_ID)
    assert inbound.identity.source.chat_type == "group"


def test_own_and_bot_messages_ignored():
    assert parse_message_create(
        _payload(author={"id": BOT_ID, "bot": False}), BOT_ID) is None
    assert parse_message_create(
        _payload(author={"id": "55", "bot": True}), BOT_ID) is None


def test_empty_content_ignored():
    assert parse_message_create(_payload(content="  "), BOT_ID) is None


def test_mention_via_mentions_array():
    inbound = parse_message_create(
        _payload(mentions=[{"id": BOT_ID}]), BOT_ID)
    assert inbound.mentions_bot is True


def test_mention_via_content_tag():
    inbound = parse_message_create(
        _payload(content=f"<@!{BOT_ID}> do the thing"), BOT_ID)
    assert inbound.mentions_bot is True


def test_user_directory_resolution():
    class _Dir:
        def resolve_internal(self, raw, surface):
            assert surface == "discord"
            return f"u_internal_{raw}"

    inbound = parse_message_create(_payload(), BOT_ID, user_directory=_Dir())
    assert inbound.identity.user_id == "u_internal_42"


def test_reply_reference_carried():
    inbound = parse_message_create(
        _payload(message_reference={"message_id": "prev-1"}), BOT_ID)
    assert inbound.reply_to == "prev-1"
