"""E4 (A6 gap 1) — Socket.IO join_session must not stream a tenant's feed to
another tenant. connect() must resolve + store the caller's identity so
join_session can check it."""
import importlib
import asyncio

import pytest


def _reload_server(monkeypatch, multitenant: bool):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true" if multitenant else "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


def _reload_server_own_ops(monkeypatch):
    """own_ops posture: public host, owner-login (cookie) gated. Posture comes
    from the explicit POLYROB_POSTURE override (WEBGATE_MULTITENANT unset)."""
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


class _FakeSio:
    def __init__(self):
        self.entered_rooms = []
        self.emitted = []

    async def enter_room(self, sid, room):
        self.entered_rooms.append((sid, room))

    async def emit(self, event, data=None, room=None):
        self.emitted.append((event, room))

    def get_environ(self, sid):
        return {"REMOTE_ADDR": "127.0.0.1"}


@pytest.mark.asyncio
async def test_cross_tenant_join_denied_in_multitenant(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=True)
    fake_sio = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake_sio)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    # PathManager uses __slots__, so patch the class method (not the instance).
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, sid: "tenant-a")
    server._socket_user["sid-b"] = "tenant-b"

    await server.join_session("sid-b", {"session_id": "some-session"})

    assert fake_sio.entered_rooms == [], "tenant-b must not join tenant-a's feed room"
    assert any(evt == "error" for evt, _room in fake_sio.emitted)


@pytest.mark.asyncio
async def test_same_tenant_join_allowed_in_multitenant(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=True)
    fake_sio = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake_sio)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, sid: "tenant-a")
    server._socket_user["sid-a"] = "tenant-a"

    await server.join_session("sid-a", {"session_id": "some-session"})

    assert fake_sio.entered_rooms, "same-tenant join must still be allowed"


@pytest.mark.asyncio
async def test_single_user_join_still_allowed(monkeypatch):
    """Posture 0 must not regress — no auth at all, exactly like today."""
    server = _reload_server(monkeypatch, multitenant=False)
    fake_sio = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake_sio)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)

    await server.join_session("sid-local", {"session_id": "any-session"})

    expected_room = server.pm().clean_session_id("any-session")
    assert ("sid-local", expected_room) in fake_sio.entered_rooms


def test_connect_resolves_local_owner_in_single_user(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=False)
    asyncio.run(server.connect("sid1", {}, auth=None))
    assert server._socket_user["sid1"] == server.webgate.local_owner_id()


def test_connect_decodes_jwt_in_multitenant(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=True)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    import jwt as pyjwt
    token = pyjwt.encode({"user_id": "tenant-a"}, "test-secret", algorithm="HS256")
    asyncio.run(server.connect("sid2", {}, auth={"token": token}))
    assert server._socket_user["sid2"] == "tenant-a"


def test_connect_anonymous_in_multitenant_without_token(monkeypatch):
    server = _reload_server(monkeypatch, multitenant=True)
    asyncio.run(server.connect("sid3", {}, auth=None))
    assert server._socket_user.get("sid3") is None


# --- own_ops (Posture 1): public host, owner-login-gated. Fixes the E4
# follow-up gap where both connect()/join_session() keyed off
# is_multitenant() only, leaving own_ops completely unauthenticated. ---


@pytest.mark.asyncio
async def test_own_ops_anonymous_join_denied(monkeypatch):
    """No token, no cookie -> connect() resolves no identity, and
    join_session must deny + never emit the feed."""
    server = _reload_server_own_ops(monkeypatch)
    fake_sio = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake_sio)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, sid: "owner-1")

    await server.connect("sid-anon", {}, auth=None)
    assert server._socket_user.get("sid-anon") is None

    await server.join_session("sid-anon", {"session_id": "some-session"})

    assert fake_sio.entered_rooms == [], "anonymous own_ops socket must not join any room"
    assert any(evt == "error" for evt, _room in fake_sio.emitted)


@pytest.mark.asyncio
async def test_own_ops_cookie_authenticated_owner_join_allowed(monkeypatch):
    """A valid owner-login JWT carried as the httponly `auth_token` cookie
    (not a client auth={"token": ...} payload) must be decoded from
    environ['HTTP_COOKIE'] and allowed to join its own session."""
    server = _reload_server_own_ops(monkeypatch)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    fake_sio = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake_sio)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, sid: "owner-1")

    import jwt as pyjwt
    token = pyjwt.encode({"user_id": "owner-1"}, "test-secret", algorithm="HS256")
    environ = {"HTTP_COOKIE": f"auth_token={token}; other=ignored"}

    await server.connect("sid-owner", environ, auth=None)
    assert server._socket_user.get("sid-owner") == "owner-1"

    await server.join_session("sid-owner", {"session_id": "some-session"})

    expected_room = server.pm().clean_session_id("some-session")
    assert ("sid-owner", expected_room) in fake_sio.entered_rooms


@pytest.mark.asyncio
async def test_own_ops_invalid_cookie_denied(monkeypatch):
    """A cookie present but signed with the wrong secret (or garbage) must
    fail-closed to anonymous, not silently authenticate."""
    server = _reload_server_own_ops(monkeypatch)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    fake_sio = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake_sio)
    monkeypatch.setattr(server, "check_rate_limit", lambda ip: True)
    monkeypatch.setattr(type(server.pm()), "get_session_user", lambda self, sid: "owner-1")

    environ = {"HTTP_COOKIE": "auth_token=not-a-real-jwt"}
    await server.connect("sid-bad", environ, auth=None)
    assert server._socket_user.get("sid-bad") is None

    await server.join_session("sid-bad", {"session_id": "some-session"})
    assert fake_sio.entered_rooms == []


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    import webview.server as server
    importlib.reload(server)
