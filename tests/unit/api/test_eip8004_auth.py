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


def test_registration_ignores_caller_origin_headers(monkeypatch):
    monkeypatch.setenv("A2A_BASE_URL", "https://operator.example")
    response = _client().get("/eip8004/registration.json", headers={
        "Host": "attacker.example", "X-Forwarded-Proto": "http"})
    assert response.status_code == 200
    assert "attacker.example" not in response.text
    assert "operator.example" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,accepted", [(None, False), ("0x" + "3" * 40, False),
                                             ("0x" + "2" * 40, True)])
async def test_feedback_is_bound_to_authenticated_wallet(caller, accepted):
    from unittest.mock import AsyncMock
    from starlette.requests import Request
    from fastapi import HTTPException
    from api.eip8004_endpoints import SubmitFeedbackBody, submit_feedback
    request = Request({"type": "http"})
    request.state.wallet_address = caller
    body = SubmitFeedbackBody(agentId=42, score=80, feedbackAuth={
        "agentId": 42, "clientAddress": "0x" + "2" * 40,
        "expiresAt": 9999999999, "nonce": "fresh", "signature": "0xab"})
    manager = AsyncMock()
    if accepted:
        await submit_feedback(body, request, manager)
        manager.submit_feedback.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await submit_feedback(body, request, manager)
        assert error.value.status_code == 403
        manager.submit_feedback.assert_not_called()
