"""Task 9 — WEBVIEW_READ_ONLY server-side enforcement.

Monitoring deployments (webview observing a headless agent) must refuse
mutations even if a client crafts the request by hand.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def _local_client(monkeypatch, read_only: bool):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("ENV", "development")
    if read_only:
        monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    else:
        monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


def test_messages_post_refused_when_read_only(monkeypatch):
    client = _local_client(monkeypatch, read_only=True)
    resp = client.post("/api/session/sess-1/messages", json={"message": "hi"})
    assert resp.status_code == 403
    assert "read-only" in resp.json()["error"].lower()


def test_messages_post_not_blocked_by_flag_when_off(monkeypatch):
    """Without the flag the endpoint proceeds to its normal auth/ownership
    path (anything but the read-only 403 shape)."""
    client = _local_client(monkeypatch, read_only=False)
    resp = client.post("/api/session/sess-1/messages", json={"message": "hi"})
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    assert "read-only" not in str(body.get("error", "")).lower()


def test_dashboard_hides_chat_input_when_read_only(monkeypatch):
    client = _local_client(monkeypatch, read_only=True)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="chat-input"' not in page.text
    assert "read-only monitoring mode" in page.text

    client2 = _local_client(monkeypatch, read_only=False)
    page2 = client2.get("/")
    assert 'id="chat-input"' in page2.text


def test_dashboard_hides_config_pickers_when_read_only(monkeypatch):
    """P2-15 (2026-07-06 UX handoff): the model/tools pickers can't start
    sessions in a read-only console — the empty state must lead with the
    monitoring hint instead of a dead config panel."""
    client = _local_client(monkeypatch, read_only=True)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="config-model"' not in page.text
    assert 'id="tools-group"' not in page.text
    assert 'href="/activity"' in page.text  # the hint points somewhere useful

    client2 = _local_client(monkeypatch, read_only=False)
    page2 = client2.get("/")
    assert 'id="config-model"' in page2.text
    assert "Pick a model and send a message" in page2.text  # P2-14 empty-state hint
