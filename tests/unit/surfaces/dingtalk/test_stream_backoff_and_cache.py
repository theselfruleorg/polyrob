"""OS10 — a session that closes at once does not reset the reconnect delay;
OS11 — the conversation cache survives a restart (DM notices after restart)."""
import asyncio

import surfaces.dingtalk.stream as st
from surfaces.dingtalk.client import Conversation, ConversationCache, DingTalkClient
from surfaces.dingtalk.stream import DingTalkStream


def _run_with(monkeypatch, durations):
    """Each session 'lasts' the given seconds; returns the waits taken."""
    stream = DingTalkStream("id", "secret")
    clock = {"t": 1000.0}
    script = list(durations)
    waits = []

    async def fake_session(handler):
        clock["t"] += script.pop(0)
        if not script:
            stream._stopping.set()

    async def fake_wait_for(aw, timeout):
        waits.append(timeout)
        aw.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(stream, "_session", fake_session)
    monkeypatch.setattr(st.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(st.time, "monotonic", lambda: clock["t"])

    async def handler(d):
        return None

    asyncio.run(stream.run(handler))
    return waits


def test_quick_normal_closes_back_off(monkeypatch):
    waits = _run_with(monkeypatch, [0.1, 0.1, 0.1, 0.1])
    assert waits == [1.0, 2.0, 4.0]


def test_a_long_session_resets_the_delay(monkeypatch):
    waits = _run_with(monkeypatch, [0.1, 0.1, st.HEALTHY_SESSION_S + 1, 0.1])
    assert waits == [1.0, 2.0, 1.0]


def test_conversation_cache_survives_a_restart(tmp_path):
    db = str(tmp_path / "dingtalk_dedup.db")
    a = ConversationCache(db_path=db)
    a.remember("cidDM1", Conversation(webhook="https://oapi.dingtalk.com/x",
                                      expires_at=1.0, is_group=False, user_id="staff9"))
    b = ConversationCache(db_path=db)
    conv = b.get("cidDM1")
    assert conv is not None and conv.is_group is False and conv.user_id == "staff9"


def test_dm_send_after_restart_uses_the_staff_id(tmp_path):
    db = str(tmp_path / "dingtalk_dedup.db")
    ConversationCache(db_path=db).remember(
        "cidDM1", Conversation(webhook="", expires_at=0.0, is_group=False, user_id="staff9"))
    client = DingTalkClient("id", "secret", conversations=ConversationCache(db_path=db))
    calls = []

    async def fake_proactive(target, text, *, is_group):
        calls.append((target, is_group))
        return {}

    client.send_proactive = fake_proactive
    asyncio.run(client.send_text("cidDM1", "hello"))
    assert calls == [("staff9", False)]


def test_harness_wires_a_persistent_cache(tmp_path):
    from surfaces.dingtalk.harness import build_dingtalk_harness
    h = build_dingtalk_harness(None, None, client_id="id", client_secret="s",
                               data_dir=str(tmp_path))
    assert h._client.conversations._db_path
