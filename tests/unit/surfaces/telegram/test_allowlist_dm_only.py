# tests/unit/surfaces/telegram/test_allowlist_dm_only.py
"""044 T7: ALLOWED_TELEGRAM_USER_IDS locks DMs. Rooms are governed by the group
model (allowlisted chat + role), never by the raw sender list — which dropped
every non-owner member before the group model could run."""
from surfaces.telegram import harness


def _upd(chat_type, uid=777):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": -100, "type": chat_type},
                                        "from": {"id": uid}, "text": "hi"}}


def test_private_chat_uses_raw_allowlist():
    assert harness.raw_allowlist_applies(_upd("private")) is True


def test_rooms_skip_raw_allowlist():
    for t in ("group", "supergroup", "channel"):
        assert harness.raw_allowlist_applies(_upd(t)) is False


def test_handle_update_source_checks_scope():
    import inspect
    src = inspect.getsource(harness.TelegramHarness.handle_update)
    assert "raw_allowlist_applies(update)" in src
