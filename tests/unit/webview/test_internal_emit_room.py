"""Task 7 — display-gap + bug fixes.

(1) POST /api/internal/emit must emit to room == clean session id — the room
    clients actually join (was ``session:{id}``, a dead room nobody joins).
(2) The orphaned Feed tab gets its button (renderer existed, tab was hidden).
(3) Dead code is gone: compute_feed_checksum, api_session_stream shadow.
(4) Exactly ONE startup hook (was two competing @on_event("startup")).
"""
import importlib
import json
from pathlib import Path

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
    server = _reload_server(monkeypatch)
    fake = _FakeSio()
    monkeypatch.setattr(server, "_sio", fake)

    from starlette.requests import Request as StarletteRequest
    body = json.dumps({"session_id": "sess-42", "event": {"type": "step", "_seq": 1}}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": "POST", "path": "/api/internal/emit",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 4321), "query_string": b"",
    }
    request = StarletteRequest(scope, receive)
    await server.internal_emit(request)

    clean = server.pm().clean_session_id("sess-42")
    assert ("feed_update", clean) in fake.emitted, (
        f"internal emit must target the joined room {clean!r}, got {fake.emitted}"
    )


def test_feed_tab_button_present():
    session_html = Path(__file__).resolve().parents[3] / "webview" / "templates" / "session.html"
    text = session_html.read_text()
    assert 'data-tab="feed"' in text, "the rich Feed renderer must be reachable via a tab button"


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


def test_repair_endpoint_runs_real_repair(monkeypatch, tmp_path):
    """/api/repair/{id} must invoke repair_session_telemetry, not fake success."""
    from fastapi.testclient import TestClient
    server = _reload_server(monkeypatch)
    session_dir = tmp_path / "sess-r"
    (session_dir / "feed").mkdir(parents=True)
    monkeypatch.setattr(type(server.pm()), "get_feed_dir",
                        lambda self, sid, user_id=None: session_dir / "feed")
    client = TestClient(server._fastapi)
    resp = client.get("/api/repair/sess-r")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "files_processed" in body["repair"], "real repair results expected"


def test_repair_endpoint_refused_in_read_only(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    server = _reload_server(monkeypatch)
    client = TestClient(server._fastapi)
    resp = client.get("/api/repair/sess-r")
    assert resp.status_code == 403
