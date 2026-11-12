"""F3 (N3) + 115 E7: the ERC-8004 HTTP surface.

- There is no off-chain feedback API (no grant signing, no submission): the
  agent reads feedback from the on-chain Reputation Registry instead.
- Validation writes need owner/admin and EIP8004_ENABLED.
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


@pytest.mark.parametrize("method,path", [
    ("post", "/eip8004/reputation/authorize"),
    ("post", "/eip8004/reputation/feedback"),
    ("post", "/eip8004/reputation/query"),
    ("get", "/eip8004/reputation/42"),
])
def test_the_local_feedback_api_is_gone(monkeypatch, method, path):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    c = _client()
    r = c.post(path, json={}) if method == "post" else c.get(path)
    assert r.status_code in (404, 405), (path, r.status_code)
    assert not [route.path for route in router.routes
                if "/reputation" in getattr(route, "path", "")]


@pytest.mark.parametrize("role,expected", [("user", 403), ("owner", 200)])
def test_validation_requests_require_privileged_caller(monkeypatch, role, expected):
    from unittest.mock import AsyncMock
    from api.eip8004_endpoints import get_validation_manager
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    app = FastAPI()
    app.include_router(router)
    manager = AsyncMock()
    manager.request_validation.return_value = {"success": True}
    app.dependency_overrides[get_validation_manager] = lambda: manager

    @app.middleware("http")
    async def authenticated(request, call_next):
        request.state.user_id = "authenticated-user"
        request.state.role = role
        return await call_next(request)

    response = TestClient(app).post("/eip8004/validation/request", json={
        "validatorAddress": "0x" + "2" * 40, "requestData": {"task": "check"}})
    assert response.status_code == expected
    assert manager.request_validation.await_count == (1 if expected == 200 else 0)


def test_write_endpoints_gated_when_disabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.post("/eip8004/validation/request", json={
        "validatorAddress": "0x" + "2" * 40, "requestData": {"task": "check"}})
    assert r.status_code == 404, r.text


def test_discovery_open_when_disabled(monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "false")
    c = _client()
    r = c.get("/eip8004/registration.json")
    assert r.status_code == 200, r.text


def test_registration_ignores_caller_origin_headers(monkeypatch):
    monkeypatch.setenv("A2A_BASE_URL", "https://operator.example")
    response = _client().get("/eip8004/registration.json", headers={
        "Host": "attacker.example", "X-Forwarded-Proto": "http"})
    assert response.status_code == 200
    assert "attacker.example" not in response.text
    assert "operator.example" in response.text
