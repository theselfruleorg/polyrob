"""M21 (2026-07-15 wallet/crypto security review): `ReputationManager` and
`ValidationManager` are process-volatile in-memory simulations — a restart
wipes all "reputation" — yet their read endpoints are served even when
`EIP8004_ENABLED` is off (M21's own fix direction: don't hard-gate the reads,
label them honest instead). Every read response here must carry an explicit
`simulated: true` marker so a consumer can't mistake local in-memory state for
an on-chain registry.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.eip8004_endpoints as eip8004_endpoints
from api.eip8004_endpoints import router


@pytest.fixture(autouse=True)
def _reset_manager_singletons():
    """The reputation/validation managers are module-level singletons — reset
    them so state (and EIP8004_AGENT_ID picked up at construction time) can't
    leak between tests."""
    eip8004_endpoints._reputation_manager = None
    eip8004_endpoints._validation_manager = None
    yield
    eip8004_endpoints._reputation_manager = None
    eip8004_endpoints._validation_manager = None


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_reputation_by_agent_id_marked_simulated_even_when_disabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.get("/eip8004/reputation/42")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["simulated"] is True
    assert body["summary"]["simulated"] is True


def test_reputation_query_marked_simulated(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.post("/eip8004/reputation/query", json={"agentId": 42})
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True


def test_validation_summary_marked_simulated_even_when_disabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.get("/eip8004/validation/summary/42")
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True


def test_validation_pending_marked_simulated(monkeypatch):
    # Seeding a pending request goes through the write endpoint, which IS
    # gated on EIP8004_ENABLED — flip it just long enough to create state
    # (no /respond call, so the request stays pending), then prove the READ
    # stays open (and honest, per-item) with EIP8004_ENABLED back off.
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_AGENT_ID", "42")
    c = _client()

    req = c.post(
        "/eip8004/validation/request",
        json={"validatorAddress": "0x" + "4" * 40, "requestData": {"k": "v"}},
    )
    assert req.status_code == 200, req.text

    monkeypatch.setenv("EIP8004_ENABLED", "false")
    r = c.get("/eip8004/validation/pending")
    assert r.status_code == 200, r.text
    body = r.json()
    # M21 restored: original bare-list shape, each item stamped simulated:true.
    assert isinstance(body, list) and body
    assert all(item["simulated"] is True for item in body)


def test_validation_validators_marked_simulated(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.get("/eip8004/validation/validators")
    assert r.status_code == 200, r.text
    body = r.json()
    # M21 restored: original bare Dict[str, str] shape, descriptions suffixed
    # " (simulated)" instead of a wrapping envelope.
    assert isinstance(body, dict) and body
    assert all(desc.endswith(" (simulated)") for desc in body.values())


def test_validation_status_marked_simulated_even_when_disabled(monkeypatch):
    # Seeding a request/response goes through the write endpoints, which ARE
    # gated on EIP8004_ENABLED (+ /respond requires admin) — flip those just
    # long enough to create state, then prove the READ stays open (and
    # honest) with EIP8004_ENABLED back off.
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_AGENT_ID", "42")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[eip8004_endpoints.require_owner_or_admin] = lambda: True
    c = TestClient(app, raise_server_exceptions=False)

    req = c.post(
        "/eip8004/validation/request",
        json={"validatorAddress": "0x" + "3" * 40, "requestData": {"k": "v"}},
    )
    assert req.status_code == 200, req.text
    request_hash = req.json()["requestHash"]

    resp = c.post(
        "/eip8004/validation/respond",
        json={"requestHash": request_hash, "response": 88},
    )
    assert resp.status_code == 200, resp.text

    monkeypatch.setenv("EIP8004_ENABLED", "false")
    status = c.get(f"/eip8004/validation/status/{request_hash}")
    assert status.status_code == 200, status.text
    assert status.json()["simulated"] is True
