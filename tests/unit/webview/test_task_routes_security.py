"""WS-3.2 (2026-07-07): the DIRECTLY-mounted /api/task/* routes must honor
webview gates.

The task router (api/task_http_api.py) is mounted straight into the webview
app, so its mutating routes (POST /api/task/sessions, …/messages, …/cancel)
used to bypass the wrapper endpoints' webgate.read_only() checks entirely,
and needed confirming they are NOT in the auth middleware's public_paths.
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


def test_own_ops_unauthenticated_task_create_denied(monkeypatch):
    """own_ops: /api/task/* is NOT a public path — an unauthenticated POST
    /api/task/sessions must be 401, never reach the handler."""
    client = _client(monkeypatch, "own_ops")
    resp = client.post("/api/task/sessions", json={"task": "hi"})
    assert resp.status_code == 401


def test_own_ops_unauthenticated_task_read_denied(monkeypatch):
    client = _client(monkeypatch, "own_ops")
    resp = client.get("/api/task/capabilities")
    assert resp.status_code == 401


def test_read_only_blocks_direct_task_mutations(monkeypatch):
    """Read-only console: POST /api/task/sessions (mounted directly, so it
    bypasses the wrapper endpoints' checks) must 403."""
    client = _client(monkeypatch, "local", read_only=True, env="development")
    resp = client.post("/api/task/sessions", json={"task": "hi"})
    assert resp.status_code == 403
    assert "read-only" in resp.text.lower()


def test_read_only_blocks_direct_task_message_and_cancel(monkeypatch):
    client = _client(monkeypatch, "local", read_only=True, env="development")
    assert client.post("/api/task/sessions/s1/messages",
                       json={"text": "hi"}).status_code == 403
    assert client.post("/api/task/sessions/s1/cancel").status_code == 403


def test_read_only_still_allows_task_reads(monkeypatch):
    """Reads through the task router must NOT be blocked by read-only."""
    client = _client(monkeypatch, "local", read_only=True, env="development")
    resp = client.get("/api/task/capabilities")
    assert resp.status_code != 403


def test_local_posture_stamps_owner_auth_state(monkeypatch):
    """WS-3 E2E finding: the local loopback operator IS the owner, so the
    middleware must stamp the canonical owner auth state — otherwise the task
    router's payment verification sees an anonymous user and 402s the local
    console's own session creation. Pinned via /api/task/sessions: the create
    handler must get past payment (503 = no TaskAgent in test container),
    never 402."""
    from unittest.mock import MagicMock
    import core.container as cc

    client = _client(monkeypatch, "local", read_only=False, env="development")
    container = MagicMock()
    container.get_agent.return_value = None
    container.get_service.return_value = None
    container.config = MagicMock(bypass_payment_for_admins=True)
    monkeypatch.setattr(cc.DependencyContainer, "get_instance",
                        classmethod(lambda cls, *a, **k: container), raising=False)
    resp = client.post("/api/task/sessions", json={"task": "hi"})
    assert resp.status_code not in (401, 402, 403)


def test_writable_console_lets_task_create_reach_handler(monkeypatch):
    """Without read-only the mutation proceeds to the handler (503 here —
    no TaskAgent in the test container — but decisively NOT the 403 gate)."""
    from unittest.mock import MagicMock
    import core.container as cc

    client = _client(monkeypatch, "local", read_only=False, env="development")
    container = MagicMock()
    container.get_agent.return_value = None
    container.get_service.return_value = None
    monkeypatch.setattr(cc.DependencyContainer, "get_instance",
                        classmethod(lambda cls, *a, **k: container), raising=False)
    resp = client.post("/api/task/sessions", json={"task": "hi"})
    assert resp.status_code == 503  # task agent unavailable — not the 403 gate
