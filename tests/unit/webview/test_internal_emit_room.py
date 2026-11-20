"""Task 7 — display-gap + bug fixes.

(1) POST /api/internal/emit must emit to room == namespaced clean session id — the room
    clients actually join (was ``session:{id}``, a dead room nobody joins).
(2) The orphaned Feed tab gets its button (renderer existed, tab was hidden).
(3) Dead code is gone: compute_feed_checksum, api_session_stream shadow.
(4) Exactly ONE startup hook (was two competing @on_event("startup")).
"""
import importlib
import json

import pytest


def _reload_server(monkeypatch):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    return importlib.reload(server)


class _FakeSio:
    def __init__(self):
        self.emitted = []

    async def emit(self, event, data=None, room=None):
        self.emitted.append((event, room))


@pytest.mark.asyncio
async def test_internal_emit_targets_joinable_room(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "internal-test-key")
    from core.security.internal_events import emit_token
    server = _reload_server(monkeypatch)
    fake = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake)

    from starlette.requests import Request as StarletteRequest
    body = json.dumps({"session_id": "sess-42", "event": {"type": "step", "_seq": 1}}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": "/api/internal/emit",
        "headers": [(b"content-type", b"application/json"),
                    (b"x-polyrob-internal-token", emit_token().encode())],
        "client": ("127.0.0.1", 4321), "query_string": b"",
    }
    request = StarletteRequest(scope, receive)
    from webview.emit_api import internal_emit  # 030 extraction
    await internal_emit(request)

    clean = server.pm().clean_session_id("sess-42")
    assert ("feed_update", "session:" + clean) in fake.emitted, (
        f"internal emit must target the joined room {clean!r}, got {fake.emitted}"
    )


def test_dead_code_removed(monkeypatch):
    server = _reload_server(monkeypatch)
    assert not hasattr(server, "compute_feed_checksum")
    assert not hasattr(server, "_feed_checksums")
    assert not hasattr(server, "api_session_stream")


def test_single_startup_hook(monkeypatch):
    server = _reload_server(monkeypatch)
    assert len(server._fastapi.router.on_startup) == 1, (
        "duplicate @on_event('startup') handlers must be merged into one"
    )
