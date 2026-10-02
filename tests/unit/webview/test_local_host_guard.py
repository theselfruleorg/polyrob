"""H15 (2026-09-23): the `local` console answers only to a loopback Host.

DNS rebinding made Origin == Host for an attacker page, so the CSRF and
Socket.IO same-origin checks passed; the Host NAME is what it cannot fake.
"""
import asyncio

import pytest

from webview.host_guard import LocalHostGuard, hostname_of, local_host_allowed


@pytest.mark.parametrize("host,name", [
    ("127.0.0.1:5050", "127.0.0.1"), ("localhost", "localhost"),
    ("LocalHost:80", "localhost"), ("[::1]:5050", "::1"), ("[::1]", "::1"),
    ("localhost.:5050", "localhost"), ("evil.example:5050", "evil.example"),
    ("::1", ""), ("[::1]x", ""), ("a:b", ""),
])
def test_hostname_of(host, name):
    assert hostname_of(host) == name


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:5050", "localhost:1",
                                  "[::1]:5050", None, ""])
def test_loopback_hosts_are_allowed(host):
    assert local_host_allowed(host, env={}) is True


@pytest.mark.parametrize("host", ["evil.example:5050", "0.0.0.0:5050", "127.0.0.1.nip.io",
                                  "localhost.evil.example", "192.168.1.5:5050", "::1"])
def test_other_hosts_are_refused(host):
    assert local_host_allowed(host, env={}) is False


def test_public_url_host_only_under_the_documented_override():
    env = {"WEBVIEW_PUBLIC_URL": "https://console.example.org"}
    assert local_host_allowed("console.example.org", env=env) is False
    env["WEBVIEW_ALLOW_LOCAL_POSTURE"] = "1"
    assert local_host_allowed("console.example.org", env=env) is True
    assert local_host_allowed("evil.example", env=env) is False


def _run(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    return sent


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(kind, host):
    return {"type": kind, "path": "/api/x", "headers": [(b"host", host.encode())]}


@pytest.fixture
def local_posture(monkeypatch):
    for k in ("POLYROB_POSTURE", "WEBGATE_MULTITENANT", "WEBGATE_HOST", "WEBVIEW_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")


def test_guard_refuses_rebound_host_with_421(local_posture):
    sent = _run(LocalHostGuard(_inner), _scope("http", "evil.example:5050"))
    assert sent[0]["status"] == 421


def test_guard_passes_loopback(local_posture):
    sent = _run(LocalHostGuard(_inner), _scope("http", "127.0.0.1:5050"))
    assert sent[0]["status"] == 200


def test_guard_closes_rebound_websocket(local_posture):
    sent = _run(LocalHostGuard(_inner), _scope("websocket", "evil.example:5050"))
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize("posture", ["own_ops", "multitenant"])
def test_guard_ignores_proxied_postures(monkeypatch, posture):
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    sent = _run(LocalHostGuard(_inner), _scope("http", "console.example.org"))
    assert sent[0]["status"] == 200


def test_guard_passes_lifespan(local_posture):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(LocalHostGuard(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_server_app_is_wrapped():
    from webview import server
    assert isinstance(server.app, LocalHostGuard)


def test_server_app_refuses_rebound_host_end_to_end(local_posture):
    from starlette.testclient import TestClient
    from webview import server
    client = TestClient(server.app, base_url="http://127.0.0.1:5050")
    assert client.get("/api/status").status_code != 421
    bad = client.get("/api/status", headers={"host": "evil.example:5050"})
    assert bad.status_code == 421
    sio = client.get("/socket.io/?EIO=4&transport=polling",
                     headers={"host": "evil.example:5050"})
    assert sio.status_code == 421
