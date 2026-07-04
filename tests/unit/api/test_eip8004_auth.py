"""F3 (N3): ERC-8004 write endpoints must not be an open signing oracle.

- /reputation/authorize signs with the agent key -> must require admin/owner.
- Write endpoints must be gated behind EIP8004_ENABLED (404 when off).
- Read/discovery (registration.json) stays open even when disabled.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.eip8004_endpoints import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_authorize_requires_admin(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    c = _client()
    r = c.post("/eip8004/reputation/authorize", json={"clientAddress": "0x" + "2" * 40})
    assert r.status_code in (401, 403), r.text


def test_write_endpoints_gated_when_disabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.post(
        "/eip8004/reputation/feedback",
        json={"agentId": 1, "score": 100, "feedbackAuth": {
            "agentId": 1, "clientAddress": "0x" + "2" * 40,
            "expiresAt": 9999999999, "nonce": "n", "signature": "0xab",
        }},
    )
    assert r.status_code == 404, r.text


def test_discovery_open_when_disabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.get("/eip8004/registration.json")
    assert r.status_code == 200, r.text
