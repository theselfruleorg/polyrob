"""WV1: in own_ops the middleware, the socket join and _check_session_ownership
apply ONE owner rule — the owner opens every session of the instance, whatever
identity tagged it (CLI ``local``, a room, a correspondent)."""
from core.security.session_tokens import SESSION_AUDIENCE
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from starlette.requests import Request
from starlette.responses import Response

SECRET = "own-ops-session-rule-regression-secret-32"


@pytest.fixture
def own_ops(monkeypatch, tmp_path):
    from webview import server, webgate
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "tokens.db"))
    monkeypatch.setattr(webgate, "requires_owner_login", lambda: True)
    monkeypatch.setattr(webgate, "is_own_ops", lambda: True)
    monkeypatch.setattr(webgate, "is_multitenant", lambda: False)
    monkeypatch.setattr(webgate, "posture", lambda: "own_ops")
    monkeypatch.setattr(webgate, "local_owner_id", lambda: "bound-owner")
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid, get_session_user=lambda sid: "local",
        get_feed_dir=lambda sid: tmp_path / "absent" / sid))
    return server


def _bearer(user_id):
    token = jwt.encode({"aud": SESSION_AUDIENCE, **{"user_id": user_id, "role": "owner", "exp": time.time() + 60,
                        "jti": uuid.uuid4().hex}}, SECRET, algorithm="HS256")
    return [(b"authorization", f"Bearer {token}".encode())]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/session/cli-chat", "/api/session/cli-chat/feed/events"])
async def test_owner_opens_a_session_another_identity_tagged(own_ops, path):
    request = Request({"type": "http", "path": path, "headers": _bearer("bound-owner"),
                       "query_string": b""})

    async def next_handler(request):
        return Response("feed")

    response = await own_ops.auth_middleware(request, next_handler)
    assert response.status_code == 200
    ok, _user, _owner = own_ops._check_session_ownership(request, "cli-chat")
    assert ok


@pytest.mark.asyncio
async def test_socket_join_admits_the_owner_on_a_local_session(own_ops, monkeypatch):
    sid = "wv1-" + uuid.uuid4().hex
    emitted, entered = [], []

    async def emit(event, payload, room=None):
        emitted.append((event, payload))

    async def enter_room(s, room):
        entered.append(room)

    monkeypatch.setattr(own_ops._sio, "emit", emit)
    monkeypatch.setattr(own_ops._sio, "enter_room", enter_room)
    monkeypatch.setattr(own_ops._sio, "get_environ", lambda s: {"REMOTE_ADDR": "127.0.0.1"})
    monkeypatch.setitem(own_ops._socket_user, sid, "bound-owner")
    try:
        await own_ops.join_session(sid, {"session_id": "cli-chat", "after_seq": 5})
        assert entered == ["session:cli-chat"], emitted
    finally:
        own_ops._client_session.pop(sid, None)
        own_ops._session_clients.pop("cli-chat", None)
        task = own_ops._watch_tasks.pop("cli-chat", None)
        if task:
            task.cancel()


@pytest.mark.asyncio
async def test_socket_join_refuses_another_identity(own_ops, monkeypatch):
    sid = "wv1-" + uuid.uuid4().hex
    emitted = []

    async def emit(event, payload, room=None):
        emitted.append((event, payload))

    monkeypatch.setattr(own_ops._sio, "emit", emit)
    monkeypatch.setattr(own_ops._sio, "get_environ", lambda s: {"REMOTE_ADDR": "127.0.0.1"})
    monkeypatch.setitem(own_ops._socket_user, sid, "customer")
    await own_ops.join_session(sid, {"session_id": "cli-chat"})
    assert emitted and emitted[-1][1]["code"] == "not_yours"


def test_middleware_refuses_another_identity_in_own_ops(own_ops):
    from webview.session_access import may_read_session_path
    pm = own_ops.pm()
    assert may_read_session_path("/session/x", "bound-owner", pm)
    assert not may_read_session_path("/session/x", "customer", pm)
    assert not may_read_session_path("/session/x", "", pm)
