"""X DM surface — pure dm_event → InboundMessage conversion (no network)."""
from surfaces.x.poller import parse_dm_event

BOT_ID = "999"


def _event(**kw):
    d = {
        "id": "1585321400689082376",
        "event_type": "MessageCreate",
        "text": "hello there",
        "sender_id": "42",
        "dm_conversation_id": "42-999",
        "created_at": "2026-07-12T00:00:00.000Z",
    }
    d.update(kw)
    return d


def test_basic_dm_parse():
    inbound = parse_dm_event(_event(), BOT_ID)
    assert inbound is not None
    assert inbound.text == "hello there"
    assert inbound.identity.source.surface_id == "x"
    assert inbound.identity.source.chat_id == "42"
    assert inbound.identity.source.chat_type == "dm"
    assert inbound.identity.raw_user_id == "42"
    assert inbound.identity.user_id == "u_x_42"
    assert inbound.idempotency_key == "1585321400689082376"
    assert inbound.mentions_bot is None


def test_own_message_ignored():
    assert parse_dm_event(_event(sender_id=BOT_ID), BOT_ID) is None


def test_non_message_create_ignored():
    assert parse_dm_event(_event(event_type="ParticipantsJoin"), BOT_ID) is None
    assert parse_dm_event(_event(event_type="ParticipantsLeave"), BOT_ID) is None


def test_empty_text_ignored():
    assert parse_dm_event(_event(text="   "), BOT_ID) is None
    assert parse_dm_event(_event(text=None), BOT_ID) is None


def test_group_dm_conversation_skipped():
    # 1:1 conversation ids are "<id>-<id>"; a bare snowflake = group DM → v1 skips.
    assert parse_dm_event(
        _event(dm_conversation_id="1585094756761149440"), BOT_ID) is None


def test_missing_conversation_id_tolerated_as_dm():
    inbound = parse_dm_event(_event(dm_conversation_id=None), BOT_ID)
    assert inbound is not None
    assert inbound.identity.source.chat_type == "dm"


def test_missing_sender_ignored():
    assert parse_dm_event(_event(sender_id=None), BOT_ID) is None


def test_user_directory_resolution():
    class _Dir:
        def resolve_internal(self, raw, surface):
            assert surface == "x"
            return f"u_internal_{raw}"

    inbound = parse_dm_event(_event(), BOT_ID, user_directory=_Dir())
    assert inbound.identity.user_id == "u_internal_42"


def test_raw_event_carried():
    ev = _event()
    inbound = parse_dm_event(ev, BOT_ID)
    assert inbound.raw is ev
