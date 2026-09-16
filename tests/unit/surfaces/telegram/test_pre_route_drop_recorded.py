"""045 lane 1: the raw-allowlist drops in handle_update leave a durable trace."""
import json

import pytest

from tests.unit.surfaces.telegram.test_harness_progress import (
    FakeBot, FakeContainer, FakeDedup, FakeUD, _drain,
)


@pytest.fixture
def tele_db(tmp_path, monkeypatch):
    p = tmp_path / "telemetry_events.db"
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(p))
    monkeypatch.setenv("SECURITY_EVENT_LOG_ENABLED", "true")
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    yield str(p)
    el._INSTANCES.clear()


def _rows(db_path):
    from core.sqlite_util import execute_retry
    return [dict(r) for r in (execute_retry(
        db_path, "SELECT kind, attrs FROM telemetry_events", (), fetch="all") or [])]


def _update(uid=999, chat=-100123, chat_type="group", text="hi"):
    return {"update_id": 1,
            "message": {"message_id": 1, "text": text,
                        "from": {"id": uid},
                        "chat": {"id": chat, "type": chat_type}}}


def _anon_update(chat=-100123, chat_type="group", text="hi"):
    """A real anonymous-admin / channel-post shape: NO 'from' key at all, so
    _tg_user_id(update) is None. Telegram sends 'sender_chat' instead of 'from'
    for an anonymous group admin or a channel linked as sender."""
    return {"update_id": 1,
            "message": {"message_id": 1, "text": text,
                        "sender_chat": {"id": chat, "type": chat_type},
                        "chat": {"id": chat, "type": chat_type}}}


@pytest.mark.asyncio
async def test_allowlist_drop_is_recorded(tele_db, monkeypatch):
    """044 T7: the raw allowlist is the DM lock — a non-allowlisted DM sender is
    dropped here; a room sender now falls through to route_inbound instead
    (raw_allowlist_applies is False for every non-private chat.type)."""
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "28436760")
    from surfaces.telegram.harness import TelegramHarness

    h = TelegramHarness.__new__(TelegramHarness)
    out = await TelegramHarness.handle_update(h, _update(uid=999, chat_type="private"))

    assert out == {"ok": True}
    rows = _rows(tele_db)
    assert len(rows) == 1
    a = json.loads(rows[0]["attrs"])
    assert rows[0]["kind"] == "access_denied"
    assert a["reason"] == "raw_allowlist"
    assert a["sender"] == "999"
    assert a["chat_type"] == "private"
    assert "hi" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_recording_failure_does_not_break_the_drop(tele_db, monkeypatch):
    """044 fix round 1 (finding 2): this must exercise the raw_allowlist drop
    branch (raw_allowlist_applies is only True for a private chat since Task 7),
    or `_boom` never runs and the assertion below passes for the wrong reason —
    the outer catch-all swallowing an unrelated AttributeError instead."""
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "28436760")
    import surfaces.telegram.harness as hz

    called = []

    def _boom(**k):
        called.append(k)
        raise RuntimeError("down")

    monkeypatch.setattr(hz, "record_pre_route_drop", _boom)
    h = hz.TelegramHarness.__new__(hz.TelegramHarness)
    out = await hz.TelegramHarness.handle_update(h, _update(uid=999, chat_type="private"))
    assert out == {"ok": True}
    assert len(called) == 1, "record_pre_route_drop (_boom) was never invoked"


@pytest.mark.asyncio
async def test_anonymous_sender_drop_is_recorded(tele_db, monkeypatch):
    """The anonymous-admin/channel-post branch (tg_id is None, non-private chat)
    must actually execute and record — not just be provable by source inspection."""
    from surfaces.telegram.harness import TelegramHarness

    h = TelegramHarness.__new__(TelegramHarness)
    out = await TelegramHarness.handle_update(h, _anon_update())

    assert out == {"ok": True}
    rows = _rows(tele_db)
    assert len(rows) == 1
    a = json.loads(rows[0]["attrs"])
    assert rows[0]["kind"] == "access_denied"
    assert a["reason"] == "anonymous_sender"
    assert a["sender"] == "anonymous"
    assert a["chat_type"] == "group"
    assert "hi" not in json.dumps(rows)


def _channel_post_update(uid=3, chat=-100999, text="announcement"):
    """A real channel_post shape: NO 'from', NO 'sender_chat' — the channel
    itself is the sender (build_inbound_message already falls back to chat_id)."""
    return {"update_id": uid, "channel_post": {
        "message_id": 7, "chat": {"id": chat, "type": "channel"}, "text": text}}


@pytest.mark.asyncio
async def test_channel_post_reaches_process_update(tele_db, monkeypatch):
    """044 fix round 1 (finding 1): a channel_post carries neither `from` nor
    `sender_chat` — _tg_user_id had no chat-id fallback (unlike
    build_inbound_message), so the anonymous-sender gate dropped every channel
    post before process_update (and therefore route_inbound) ever ran. Proven
    end-to-end via handle_update, not just via build_inbound_message directly."""
    import surfaces.telegram.inbound as inbound_mod
    from surfaces.telegram.harness import TelegramHarness

    calls = []

    async def fake_process_update(*a, **k):
        calls.append((a, k))
        return None  # channel_post is a redelivery/no-route stand-in here

    monkeypatch.setattr(inbound_mod, "process_update", fake_process_update)

    # 044 I2: an unlisted room is now dropped BEFORE process_update (no paid
    # voice download, no user-directory row for a stranger). Allow the channel so
    # this still tests what it is about — the anonymous-sender exemption.
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    _home = os.path.dirname(tele_db)
    monkeypatch.setenv("POLYROB_DATA_DIR", _home)
    GroupAllowlist(os.path.join(_home, "group_allowlist.db")).allow("telegram", "-100999")

    bot = FakeBot()
    harness = TelegramHarness(
        bot, FakeContainer(), object(),
        webhook_base=None, dedup=FakeDedup(), user_directory=FakeUD(),
    )
    out = await harness.handle_update(_channel_post_update())
    await _drain()

    assert out == {"ok": True}
    assert len(calls) == 1, "channel_post never reached process_update"
    # No pre-route drop means no telemetry row was ever written — the
    # telemetry_events table itself doesn't exist yet in that case.
    try:
        rows = _rows(tele_db)
    except Exception:
        rows = []
    assert rows == [], f"channel_post was dropped pre-route instead of routed: {rows}"
