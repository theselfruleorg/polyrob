"""Task 5 — Socket.IO activity room gating (join_activity / leave_activity).

The global stream is cross-tenant, so join_activity is owner/admin-gated in
every non-local posture, and the hub starts lazily / stops when the room
empties.
"""
import importlib

import pytest


def _reload_server(monkeypatch, posture: str):
    for key in ("WEBGATE_MULTITENANT", "POLYROB_POSTURE", "WEBVIEW_ACTIVITY_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("ENV", "development")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as server
    importlib.reload(server)
    return server


class _FakeSio:
    def __init__(self):
        self.entered_rooms = []
        self.left_rooms = []
        self.emitted = []

    async def enter_room(self, sid, room):
        self.entered_rooms.append((sid, room))

    async def leave_room(self, sid, room):
        self.left_rooms.append((sid, room))

    async def emit(self, event, data=None, room=None):
        self.emitted.append((event, room))

    async def disconnect(self, sid):
        pass

    def get_environ(self, sid):
        return {"REMOTE_ADDR": "127.0.0.1"}


@pytest.fixture(autouse=True)
def _fresh_hub():
    import webview.activity as activity
    yield
    hub = activity.get_hub()
    hub.stop()
    activity._hub = None


@pytest.mark.asyncio
async def test_own_ops_non_owner_denied(monkeypatch):
    server = _reload_server(monkeypatch, "own_ops")
    fake = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    server._socket_user["sid-x"] = "not-the-owner"
    server._socket_tier["sid-x"] = None

    await server.join_activity("sid-x", {})

    assert ("sid-x", "activity") not in fake.entered_rooms
    assert any(evt == "error" for evt, _ in fake.emitted)


@pytest.mark.asyncio
async def test_own_ops_owner_allowed_gets_snapshot(monkeypatch):
    server = _reload_server(monkeypatch, "own_ops")
    import webview.webgate as wg
    import webview.activity as activity
    fake = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    server._socket_user["sid-o"] = wg.local_owner_id()
    server._socket_tier["sid-o"] = "admin"

    await server.join_activity("sid-o", {})

    assert ("sid-o", "activity") in fake.entered_rooms
    assert any(evt == "activity_snapshot" for evt, _ in fake.emitted)
    assert activity.get_hub().started is True

    # last client leaves → hub stops
    await server.leave_activity("sid-o")
    assert activity.get_hub().started is False


@pytest.mark.asyncio
async def test_local_open_and_flag_off_denied(monkeypatch):
    server = _reload_server(monkeypatch, "local")
    fake = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)

    await server.join_activity("sid-l", {})
    assert ("sid-l", "activity") in fake.entered_rooms

    monkeypatch.setenv("WEBVIEW_ACTIVITY_ENABLED", "false")
    fake2 = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake2)
    await server.join_activity("sid-l2", {})
    assert ("sid-l2", "activity") not in fake2.entered_rooms
    assert any(evt == "error" for evt, _ in fake2.emitted)

    # drain the hub inside this test's loop (no pending-task warnings)
    await server.leave_activity("sid-l")


@pytest.mark.asyncio
async def test_disconnect_cleans_activity_membership(monkeypatch):
    server = _reload_server(monkeypatch, "local")
    import webview.activity as activity
    fake = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)

    await server.join_activity("sid-d", {})
    assert activity.get_hub().started is True
    await server.disconnect("sid-d")
    assert activity.get_hub().started is False
