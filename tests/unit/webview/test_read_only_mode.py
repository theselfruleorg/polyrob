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
    """Since 043 W3 the refusal comes from the ONE dependency
    (``webgate.read_only_guard``), so the body is FastAPI's ``{"detail": …}``
    rather than the handler's old ``{"success": false, "error": …}`` — the
    handler never runs at all now."""
    client = _local_client(monkeypatch, read_only=True)
    resp = client.post("/api/session/sess-1/messages", json={"message": "hi"})
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"].lower()
    assert "WEBVIEW_READ_ONLY" in resp.json()["detail"]


def test_messages_post_not_blocked_by_flag_when_off(monkeypatch):
    """Without the flag the endpoint proceeds to its normal auth/ownership
    path (anything but the read-only 403 shape)."""
    client = _local_client(monkeypatch, read_only=False)
    resp = client.post("/api/session/sess-1/messages", json={"message": "hi"})
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    assert "read-only" not in str(body.get("error", "")).lower()


# The read-only DASHBOARD-chrome tests (chat input / model+tools pickers hidden,
# the "read-only monitoring mode" banner) were removed in 043 phase 2 (§9): they
# asserted markers of the deleted session.html dashboard, which no longer renders
# at `/`. The server-side read-only ENFORCEMENT — the real guarantee — stays
# covered above by test_messages_post_refused_when_read_only. Re-covering the
# read-only chat page's hidden composer is the new shell's test to write (the
# webview conftest documents this KNOWN GAP for the C6 `/` replacement).
