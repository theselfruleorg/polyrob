"""Week shipment review: admin writers still need their non-database services."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api import admin_endpoints as admin


@pytest.fixture
def services(monkeypatch):
    from core.container import DependencyContainer

    db = SimpleNamespace(fetch_one=AsyncMock(), execute=AsyncMock())
    registered = {"database_manager": db}
    container = SimpleNamespace(get_service=registered.get)
    monkeypatch.setattr(DependencyContainer, "get_instance", lambda: container)
    monkeypatch.setattr(admin, "get_audit_logger", AsyncMock(return_value=None))
    return db, registered


@pytest.mark.asyncio
async def test_verify_token_uses_registered_alchemy_service(services):
    db, registered = services
    db.fetch_one.return_value = {
        "user_id": "u1", "wallet_address": "0x" + "1" * 40,
        "tier": "free", "den_token_count": 0,
    }
    alchemy = SimpleNamespace(alchemy_check_token=AsyncMock(return_value={
        "status": "success", "token_count": 1, "token_ids": [],
    }))
    registered["alchemy"] = alchemy
    request = SimpleNamespace(state=SimpleNamespace(user_id="admin"), headers={}, client=None)
    result = await admin.verify_user_token(request, "u1")
    assert result.new_tier == "holder"
    alchemy.alchemy_check_token.assert_awaited_once()
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_charged_resolution_requires_an_actual_deduction(services, available):
    db, registered = services
    db.fetch_one.return_value = {
        "id": 7, "user_id": "u1", "credits_owed": 50, "status": "pending",
    }
    balance = SimpleNamespace(deduct_credits=AsyncMock(return_value=True))
    if available:
        registered["balance_manager"] = balance
    request = SimpleNamespace(state=SimpleNamespace(user_id="admin"))
    resolution = admin.ResolveBillingFailureRequest(resolution="charged", notes="retry")
    if available:
        result = await admin.resolve_billing_failure(request, 7, resolution)
        assert result["success"] is True
        balance.deduct_credits.assert_awaited_once()
        db.execute.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await admin.resolve_billing_failure(request, 7, resolution)
        assert exc.value.status_code == 503
        db.execute.assert_not_awaited()
