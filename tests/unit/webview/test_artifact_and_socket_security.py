import time

import jwt
import pytest
from starlette.requests import Request
from starlette.responses import Response


@pytest.mark.asyncio
async def test_artifact_sandbox_is_enforced_on_direct_navigation():
    from webview.server import add_security_headers

    async def next_handler(_):
        return Response("<script>fetch('/api/sessions')</script>", media_type="text/html")

    request = Request({"type": "http", "path": "/api/session/s/workspace/serve/app.html",
                       "headers": [], "scheme": "https", "server": ("console.test", 443)})
    response = await add_security_headers(request, next_handler)
    directives = response.headers["Content-Security-Policy"].split(";")
    sandbox = next(d.strip() for d in directives if d.strip().startswith("sandbox"))
    assert sandbox == "sandbox allow-scripts"


def test_raw_html_query_route_has_response_sandbox(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from webview import server

    (tmp_path / "app.html").write_text("<h1>artifact</h1>")
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid,
        get_session_user=lambda sid: "owner",
        get_workspace_dir=lambda sid, user_id: tmp_path,
    ))
    app = FastAPI()
    app.middleware("http")(server.add_security_headers)
    # Reuse the registered application route so a path/query contract change
    # cannot leave this regression checking an invented endpoint.
    app.router.routes.append(next(route for route in server._fastapi.routes
                                 if getattr(route, "endpoint", None) is server.api_workspace_file))
    response = TestClient(app).get("/api/session/session/workspace/file", params={"path": "app.html"})
    assert response.status_code == 200
    assert response.text == "<h1>artifact</h1>"
    assert response.headers["content-type"].startswith("text/html")
    directives = [part.strip() for part in response.headers["content-security-policy"].split(";")]
    assert "sandbox" in directives
    assert not any("allow-same-origin" in part for part in directives)


def test_revoked_cookie_cannot_reconnect_over_socket(monkeypatch, tmp_path):
    from core.token_denylist import get_token_denylist
    from webview.server import _decode_socket_payload

    secret = "test-secret-for-security-regressions-32"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "tokens.db"))
    claims = {"user_id": "owner", "tier": "admin", "jti": "logout-token", "exp": time.time() + 60}
    token = jwt.encode(claims, secret, algorithm="HS256")
    assert _decode_socket_payload(token)["user_id"] == "owner"
    get_token_denylist().revoke(claims["jti"], expires_at=claims["exp"])
    assert _decode_socket_payload(token) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [True, False])
async def test_active_stream_disconnects_after_expiry_or_logout(monkeypatch, expired):
    import asyncio
    from webview.socket_auth import SocketAuthMonitor

    disconnected = asyncio.Event()

    async def disconnect(sid):
        assert sid == "socket"
        disconnected.set()

    monkeypatch.setattr("webview.socket_auth.jti_is_revoked", lambda claims: not expired)
    monitor = SocketAuthMonitor(disconnect, interval=0.001)
    monitor.register("socket", {"user_id": "owner", "exp": time.time() + (-1 if expired else 60)})
    await asyncio.wait_for(disconnected.wait(), timeout=1)
    assert not monitor.tasks


@pytest.mark.asyncio
async def test_replacing_socket_watcher_keeps_new_task_registered():
    import asyncio
    from webview.socket_auth import SocketAuthMonitor

    async def disconnect(sid):
        pass

    monitor = SocketAuthMonitor(disconnect, interval=10)
    claims = {"user_id": "owner", "exp": time.time() + 60}
    monitor.register("socket", claims)
    await asyncio.sleep(0)
    monitor.register("socket", claims)
    replacement = monitor.tasks["socket"]
    await asyncio.sleep(0)
    assert monitor.tasks["socket"] is replacement
    monitor.remove("socket")
    await asyncio.gather(replacement, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", ["bad", float("nan"), float("inf")])
async def test_invalid_expiry_disconnects_instead_of_stopping_monitor(expiry):
    import asyncio
    from webview.socket_auth import SocketAuthMonitor

    disconnected = asyncio.Event()

    async def disconnect(sid):
        disconnected.set()

    monitor = SocketAuthMonitor(disconnect, interval=0.001)
    monitor.register("socket", {"user_id": "owner", "exp": expiry})
    await asyncio.wait_for(disconnected.wait(), timeout=1)
    assert not monitor.tasks


@pytest.mark.asyncio
async def test_monitor_retries_failed_disconnect():
    import asyncio
    from webview.socket_auth import SocketAuthMonitor

    disconnected = asyncio.Event()
    attempts = 0

    async def disconnect(sid):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transport failure")
        disconnected.set()

    monitor = SocketAuthMonitor(disconnect, interval=0.001)
    monitor.register("socket", {"user_id": "owner", "exp": 1})
    await asyncio.wait_for(disconnected.wait(), timeout=1)
    assert attempts == 2
    assert not monitor.tasks


@pytest.mark.asyncio
async def test_logout_does_not_claim_success_when_revocation_write_fails(monkeypatch):
    from core.token_denylist import RevocationUnavailable
    from webview.server import logout

    def fail(token):
        raise RevocationUnavailable("disk full")

    monkeypatch.setattr("core.token_denylist.revoke_cookie_token", fail)
    request = Request({"type": "http", "path": "/logout", "headers": [
        (b"cookie", b"auth_token=test-token")
    ]})
    response = await logout(request)
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert b"not been revoked" in response.body


@pytest.mark.parametrize("missing", ["exp", "jti"])
def test_socket_refuses_legacy_unrevocable_or_nonexpiring_tokens(monkeypatch, missing):
    from webview.server import _decode_socket_payload

    secret = "session-token-security-test-secret-32"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    claims = {"user_id": "owner", "exp": time.time() + 60, "jti": "session"}
    del claims[missing]
    assert _decode_socket_payload(jwt.encode(claims, secret)) == {}
