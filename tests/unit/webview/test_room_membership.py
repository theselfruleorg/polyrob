"""098 TS-22: room membership is a set and a disconnect during a join's await
undoes the join, so no feed watcher (or activity hub) leaks."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def srv(monkeypatch, tmp_path):
    from webview import server
    monkeypatch.setattr(server.webgate, "requires_owner_login", lambda: False)
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid, get_session_user=lambda sid: "local",
        get_feed_dir=lambda sid: tmp_path / "sessions" / sid / "feed",
        find_feed_dir=lambda sid, user_id=None: None))
    gate = {"ev": None}
    left = []

    async def emit(*a, **k):
        return None

    async def enter_room(sid, room):
        if gate["ev"] is not None:
            await gate["ev"].wait()

    async def leave_room(sid, room):
        left.append((sid, room))

    async def watcher(session_id):
        await asyncio.sleep(3600)

    monkeypatch.setattr(server._sio, "emit", emit)
    monkeypatch.setattr(server._sio, "enter_room", enter_room)
    monkeypatch.setattr(server._sio, "leave_room", leave_room)
    monkeypatch.setattr(server._sio, "get_environ", lambda sid: {"REMOTE_ADDR": "127.0.0.1"})
    monkeypatch.setattr(server, "_feed_watcher", watcher)
    server._gate, server._left = gate, left
    return server


def _cleanup(server, sid, chat):
    t = server._watch_tasks.pop(chat, None)
    if t:
        t.cancel()
    server._client_session.pop(sid, None)
    server._session_clients.pop(chat, None)
    server.forget(sid)


@pytest.mark.asyncio
async def test_disconnect_during_join_does_not_leak_watcher(srv):
    sid, chat = "m-" + uuid.uuid4().hex, "m-chat-" + uuid.uuid4().hex
    srv._gate["ev"] = asyncio.Event()
    try:
        join = asyncio.create_task(srv.join_session(sid, {"session_id": chat}))
        await asyncio.sleep(0)
        await srv.disconnect(sid)
        srv._gate["ev"].set()
        await join
        assert chat not in srv._session_clients
        assert chat not in srv._watch_tasks
        assert sid not in srv._client_session
    finally:
        _cleanup(srv, sid, chat)


@pytest.mark.asyncio
async def test_disconnect_during_join_activity_stops_hub(srv, monkeypatch):
    monkeypatch.setattr(srv.webgate, "activity_enabled", lambda: True)
    sid = "a-" + uuid.uuid4().hex
    srv._gate["ev"] = asyncio.Event()
    started = []
    import webview.activity as act
    monkeypatch.setattr(act, "get_hub", lambda: SimpleNamespace(
        start=lambda sio: started.append(1), aclose=lambda: asyncio.sleep(0)))
    join = asyncio.create_task(srv.join_activity(sid))
    await asyncio.sleep(0)
    await srv.disconnect(sid)
    srv._gate["ev"].set()
    await join
    assert sid not in srv._activity_clients and started == []
    srv.forget(sid)


@pytest.mark.asyncio
async def test_membership_is_idempotent_on_double_join(srv):
    sid, chat = "d-" + uuid.uuid4().hex, "d-chat-" + uuid.uuid4().hex
    try:
        await srv.join_session(sid, {"session_id": chat})
        await srv.join_session(sid, {"session_id": chat})
        assert srv._session_clients[chat] == {sid}
        await srv.disconnect(sid)
        assert chat not in srv._session_clients
        await asyncio.sleep(0)
        assert chat not in srv._watch_tasks
    finally:
        _cleanup(srv, sid, chat)
