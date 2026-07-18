"""Regression (P1 finalization): webview auth defaulted OFF whenever ENV=development,
even in own_ops/multitenant posture — a staging deployment (ENV=development) had NO
auth on the console. The default must key on posture (auth-required for own_ops/
multitenant), not ENV.
"""
import importlib

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, posture, env):
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", posture)
    monkeypatch.setenv("ENV", env)
    monkeypatch.delenv("WEBVIEW_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("WEBVIEW_READ_ONLY", raising=False)
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    return TestClient(srv._fastapi, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    import webview.server as server
    importlib.reload(server)


def test_multitenant_dev_still_requires_auth(monkeypatch):
    """multitenant + ENV=development + WEBVIEW_AUTH_ENABLED unset: a protected route
    must NOT be served to an unauthenticated caller."""
    client = _client(monkeypatch, "multitenant", "development")
    resp = client.get("/api/task/capabilities")
    assert resp.status_code == 401, (
        f"multitenant dev must still require auth, got {resp.status_code}"
    )
