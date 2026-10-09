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
        get_feed_dir=lambda sid: tmp_path / "sessions" / sid / "feed",
        find_feed_dir=lambda sid, user_id=None: None))

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


def test_the_read_only_lookup_creates_nothing_and_never_sleeps(tmp_path, monkeypatch):
    """WEB-9: the REAL path manager's console lookup — `get_feed_dir` made
    `_anonymous_/<id>/feed` after ~0.3 s of blocking sleeps."""
    import time
    from agents.task.path import PathManager
    mgr = PathManager(data_root=str(tmp_path / "task"))
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert mgr.find_feed_dir("attackerchosen123") is None
    assert mgr.find_session_root("attackerchosen123", "local") is None
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    # a real session is found once the agent made it
    (tmp_path / "task" / "local" / "realsess" / "feed").mkdir(parents=True)
    assert mgr.find_feed_dir("realsess", "local") == (tmp_path / "task" / "local" / "realsess" / "feed").resolve()


@pytest.mark.asyncio
async def test_the_watcher_gives_up_on_an_id_that_never_appears(local_server, monkeypatch):
    server = local_server
    import webview.feed_routes as feed_routes
    monkeypatch.setattr(feed_routes, "FEED_WAIT_POLLS", 2)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(server.asyncio, "sleep", lambda s: real_sleep(0))
    await asyncio.wait_for(server._feed_watcher("web9-never"), timeout=5)
