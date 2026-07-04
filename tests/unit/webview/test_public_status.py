"""Unit tests for the posture-aware public `/` status route (B2).

- own_ops/multitenant, unauthenticated: minimal public status page + /api/status.
- local (Posture 0): unchanged full dashboard (regression guard).
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def own_ops_client(monkeypatch):
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


@pytest.fixture
def local_client(monkeypatch):
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


def test_own_ops_root_is_status_page_when_unauthenticated(own_ops_client):
    resp = own_ops_client.get("/")
    assert resp.status_code == 200
    assert "POLYROB is live" in resp.text
    # Must NOT leak dashboard chrome (chat input, new-session control) to an
    # unauthenticated caller on a public posture. These ids are real DOM
    # markers pulled from webview/templates/session.html (the dashboard
    # template) and are absent from status.html.
    assert 'id="chat-input"' not in resp.text
    assert 'id="new-session-btn"' not in resp.text
    # Positive confirmation this response is actually the status page.
    assert "status-page" in resp.text


def test_own_ops_status_json_endpoint(own_ops_client):
    resp = own_ops_client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "live"
    assert "version" in body
    assert "uptime_seconds" in body
    assert isinstance(body["uptime_seconds"], int)


def test_local_root_is_full_dashboard(local_client):
    resp = local_client.get("/")
    assert resp.status_code == 200
    assert "POLYROB is live" not in resp.text  # not the minimal status page
