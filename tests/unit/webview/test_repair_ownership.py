"""Regression (P0): /api/repair/{session_id} mutates a session's telemetry
(feed/llm_usage files) but had NO ownership check — only a read_only() gate. Any
authenticated tenant in multitenant/own_ops posture could corrupt another
tenant's session data. It must now enforce _check_session_ownership.

043 W2: the route is a POST (it was a GET — a mutation any link or prefetch
could trigger). The read-only refusal is the shared guard now; see
test_repair_is_post.py for the method contract.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, posture: str, read_only: bool = False, env: str = "production"):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("ENV", env)
    monkeypatch.delenv("WEBVIEW_AUTH_ENABLED", raising=False)
    if read_only:
        monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    else:
        monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi)


@pytest.fixture(autouse=True)
def _restore_server(monkeypatch):
    yield
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.server as server
    importlib.reload(server)


def test_multitenant_unauthenticated_repair_denied(monkeypatch):
    """multitenant: an unauthenticated caller must NOT repair (mutate) a session."""
    client = _client(monkeypatch, "multitenant")
    resp = client.post("/api/repair/sess-belonging-to-someone-else")
    assert resp.status_code in (401, 403), (
        f"unauthenticated repair must be denied, got {resp.status_code}"
    )


def test_read_only_still_blocks_repair(monkeypatch):
    """The pre-existing read-only gate must still fire (defence in depth)."""
    client = _client(monkeypatch, "local", read_only=True, env="development")
    resp = client.post("/api/repair/sess-x")
    assert resp.status_code == 403
    assert "read-only" in resp.text.lower()
