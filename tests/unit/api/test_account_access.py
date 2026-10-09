from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.account_access import enforce_account_access


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_method", [None, "x402"])
@pytest.mark.parametrize("path", ["/a2a/rpc", "/v1/chat/completions", "/api/chat", "/task/start"])
async def test_blocked_accounts_cannot_reach_any_task_surface(monkeypatch, payment_method, path):
    request = Request({"type": "http", "path": path, "headers": []})
    request.state.user_id = "usr_blocked"
    request.state.authenticated = True
    request.state.payment_method = payment_method
    db = SimpleNamespace(fetch_one=AsyncMock(return_value={"is_blocked": 1}))
    monkeypatch.setattr("api.dependencies.require_service", lambda *a, **kw: db)
    downstream = AsyncMock(return_value=Response())
    result = await enforce_account_access(request, downstream)
    assert result.status_code == 403
    downstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_block_store_refuses_access(monkeypatch):
    request = Request({"type": "http", "path": "/v1/chat/completions", "headers": []})
    request.state.user_id, request.state.authenticated = "usr_1", True
    db = SimpleNamespace(fetch_one=AsyncMock(side_effect=OSError("unreadable")))
    monkeypatch.setattr("api.dependencies.require_service", lambda *a, **kw: db)
    downstream = AsyncMock(return_value=Response())
    assert (await enforce_account_access(request, downstream)).status_code == 503
    downstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/a2a/rpc", "/v1/chat/completions", "/api/chat/message", "/task/sessions"])
async def test_welcome_credits_do_not_bypass_compute_tier(monkeypatch, path):
    from api.payment_verification import verify_payment_for_request
    from core.container import DependencyContainer
    from fastapi import HTTPException
    request = Request({"type": "http", "path": path, "headers": []})
    request.state.user_id, request.state.role, request.state.tier = "u1", "user", "free_access"
    tier = SimpleNamespace(get_user_tier=AsyncMock(return_value="free"))
    balance = SimpleNamespace(has_sufficient_balance=AsyncMock(return_value=True))
    services = {"tier_manager": tier, "balance_manager": balance}
    container = SimpleNamespace(config=None, get_service=services.get)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setattr(DependencyContainer, "get_instance", lambda: container)
    with pytest.raises(HTTPException) as error:
        await verify_payment_for_request(request)
    assert error.value.status_code == 403
    balance.has_sufficient_balance.assert_not_awaited()
