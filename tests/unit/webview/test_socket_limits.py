"""070 W0.17 — a socket counts once, the key is the owner, a refusal is a code."""
import asyncio
import uuid

import pytest

from webview import server, socket_limits as sl


def test_one_count_per_sid():
    assert sl.first_join("sid-a") is True
    assert sl.first_join("sid-a") is False
    sl.forget("sid-a")
    assert sl.first_join("sid-a") is True
    sl.forget("sid-a")


def test_key_is_owner_when_known():
    environ = {"REMOTE_ADDR": "127.0.0.1"}
    assert sl.limit_key("s1", environ, {"s1": "owner-1"}) == "u:owner-1"
    assert sl.limit_key("s2", environ, {"s1": "owner-1"}) == "ip:127.0.0.1"
    assert sl.limit_key("s3", None, {}) == "ip:unknown"


def test_refusal_has_code_and_no_prose():
    assert sl.refusal("rate_limited", 60) == {"code": "rate_limited", "retry_after": 60}
    assert sl.refusal("no_chat") == {"code": "no_chat", "retry_after": None}


def test_the_limit_is_thirty_per_minute():
    assert server.RATE_LIMIT_MAX_CONNECTIONS == 30 == sl.JOIN_LIMIT_PER_MIN


@pytest.fixture()
def fake_sio(monkeypatch):
    sent, dropped = [], []

    async def emit(event, body, room=None):
        sent.append((room, event, body))

    async def disconnect(sid):
        dropped.append(sid)

    async def enter_room(sid, room):
        return None

    monkeypatch.setattr(server._sio, "emit", emit)
    monkeypatch.setattr(server._sio, "disconnect", disconnect)
    monkeypatch.setattr(server._sio, "enter_room", enter_room)
    ip = "10.9." + uuid.uuid4().hex[:6]
    monkeypatch.setattr(server._sio, "get_environ", lambda sid: {"REMOTE_ADDR": ip})
    monkeypatch.setattr(server.webgate, "activity_enabled", lambda: False)
    return sent, dropped


def test_rate_limited_does_not_disconnect(fake_sio):
    """31 joins from 31 sockets on one IP: the 31st is refused with a code and
    stays connected; a second join on an already-counted socket is free."""
    sent, dropped = fake_sio
    sids = [f"rl-{i}" for i in range(31)]
    try:
        for sid in sids:
            asyncio.run(server.join_activity(sid, {}))
        codes = [body["code"] for _room, _e, body in sent]
        assert codes.count("rate_limited") == 1
        assert codes.count("activity_off") == 30
        refused = [b for _r, _e, b in sent if b["code"] == "rate_limited"][0]
        assert refused == {"code": "rate_limited", "retry_after": server.RATE_LIMIT_WINDOW}
        assert dropped == []
        # the same socket joining its session room costs no second count
        sent.clear()
        asyncio.run(server.join_session(sids[0], {}))
        assert sent[0][2] == {"code": "no_chat", "retry_after": None}
    finally:
        for sid in sids:
            sl.forget(sid)
