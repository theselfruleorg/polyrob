"""WS3: a dead feed watcher is restarted on the next join.
WS8: a console read never creates a session tree (the watcher waits instead)."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture
def local_server(monkeypatch, tmp_path):
    from webview import server
    monkeypatch.setattr(server.webgate, "requires_owner_login", lambda: False)
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid, get_session_user=lambda sid: "local",
        get_feed_dir=lambda sid: tmp_path / "sessions" / sid / "feed"))

    async def emit(*a, **k):
        return None

    async def enter_room(sid, room):
        return None

    monkeypatch.setattr(server._sio, "emit", emit)
    monkeypatch.setattr(server._sio, "enter_room", enter_room)
    monkeypatch.setattr(server._sio, "get_environ", lambda sid: {"REMOTE_ADDR": "127.0.0.1"})
    return server


@pytest.mark.asyncio
async def test_a_dead_watcher_is_restarted_on_join(local_server, monkeypatch):
    server = local_server
    started = []

    async def fake_watcher(session_id):
        started.append(session_id)
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "_feed_watcher", fake_watcher)
    sid, chat = "ws3-" + uuid.uuid4().hex, "ws3-chat"

    async def dead():
        return None

    corpse = asyncio.create_task(dead())
    await corpse
    server._watch_tasks[chat] = corpse
    try:
        await server.join_session(sid, {"session_id": chat})
        await asyncio.sleep(0)
        live = server._watch_tasks[chat]
        assert live is not corpse and not live.done()
        assert started == [chat]
    finally:
        task = server._watch_tasks.pop(chat, None)
        if task:
            task.cancel()
        server._client_session.pop(sid, None)
        server._session_clients.pop(chat, None)
        server.forget(sid)


@pytest.mark.asyncio
async def test_the_watcher_never_creates_a_session_tree(local_server, tmp_path):
    server = local_server
    task = asyncio.create_task(server._feed_watcher("ws8-never-made"))
    await asyncio.sleep(0.05)
    try:
        assert not (tmp_path / "sessions").exists()
        assert not task.done()  # it waits for the agent to make the feed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
