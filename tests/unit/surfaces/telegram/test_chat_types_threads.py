# tests/unit/surfaces/telegram/test_chat_types_threads.py
from surfaces.telegram.inbound import _chat_type, build_inbound_message
from surfaces.telegram.triggers import effective_thread_id


def test_chat_type_values():
    assert _chat_type("private") == "dm"
    assert _chat_type("group") == "group"
    assert _chat_type("supergroup") == "supergroup"
    assert _chat_type("channel") == "channel"


def test_forum_general_topic_is_1():
    msg = {"chat": {"id": -1, "type": "supergroup", "is_forum": True}}
    assert effective_thread_id(msg) == "1"


def test_forum_topic_uses_thread_id():
    msg = {"chat": {"id": -1, "type": "supergroup", "is_forum": True},
           "message_thread_id": 42, "is_topic_message": True}
    assert effective_thread_id(msg) == "42"


def test_plain_group_reply_anchor_is_not_a_thread():
    msg = {"chat": {"id": -1, "type": "group"}, "message_thread_id": 99,
           "reply_to_message": {"message_id": 99}}
    assert effective_thread_id(msg) is None


def test_channel_post_is_routable(tmp_path):
    from tests.unit.surfaces.telegram.test_inbound import _deps
    _, ud, _ = _deps(tmp_path)
    upd = {"update_id": 3, "channel_post": {"message_id": 7, "chat": {"id": -100, "type": "channel"},
                                            "text": "announcement"}}
    inbound = build_inbound_message(upd, ud, bot_username="robbot")
    assert inbound is not None
    assert inbound.identity.source.chat_type == "channel"


def test_room_types_agree_with_public_session_predicate():
    """044 T9: room_keys.is_group_session_key's _ROOM_TYPES and binding.py's
    `chat_type != "dm"` public-session predicate must agree for every value
    _chat_type can emit — a mismatch would make a room's public-session marking
    diverge from its group-gated session key."""
    from core.surfaces.room_keys import is_group_session_key
    for raw in ("private", "group", "supergroup", "channel", "some_unknown_type"):
        ct = _chat_type(raw)
        is_public_by_predicate = ct != "dm"       # binding.py::bind_chat_surface
        key = f"agent:main:telegram:{ct}:-100123"
        assert is_group_session_key(key) is is_public_by_predicate, ct
