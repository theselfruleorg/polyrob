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
@pytest.mark.parametrize("actor,target,target_role,allowed", [
    ("admin", "peer", "admin", False), ("owner", "peer", "admin", True),
    ("admin", "tenant", "user", True), ("owner", "owner", "user", False),
    ("admin", "admin", "user", False), ("owner", "peer", "owner", False),
])
async def test_block_preserves_administrative_hierarchy(services, monkeypatch,
                                                       actor, target, target_role, allowed):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    db, _ = services
    db.fetch_one.return_value = {"role": target_role, "wallet_address": None}
    request = SimpleNamespace(state=SimpleNamespace(user_id=actor, role=actor),
                              headers={}, client=None)
    body = admin.BlockUserRequest(reason="access revoked")
    if allowed:
        assert (await admin.block_user(request, target, body))["success"]
        db.execute.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await admin.block_user(request, target, body)
        assert exc.value.status_code == 403
        db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_charged_resolution_requires_an_actual_deduction(services, available, tmp_path):
    from modules.database.connection import DatabaseConnection
    _, registered = services
    conn = DatabaseConnection(tmp_path / "bot.db")
    await conn.connect()
    await conn.execute("CREATE TABLE billing_failures (id INTEGER PRIMARY KEY, user_id TEXT, "
                       "status TEXT, session_id TEXT, request_id TEXT UNIQUE, credits_owed INTEGER, "
                       "api_cost_usd REAL, model TEXT, created_at TEXT, resolved_at TEXT, "
                       "resolution_notes TEXT)")
    await conn.execute("INSERT INTO billing_failures VALUES (7, 'u1', 'pending', 's', "
                       "'legacy-call', 50, 0, NULL, 'now', NULL, NULL)")
    registered["database_manager"] = conn
    balance = SimpleNamespace(deduct_credits=AsyncMock(return_value=True))
    if available:
        registered["balance_manager"] = balance
    request = SimpleNamespace(state=SimpleNamespace(user_id="admin"))
    resolution = admin.ResolveBillingFailureRequest(resolution="charged", notes="retry")
    try:
        if available:
            result = await admin.resolve_billing_failure(request, 7, resolution)
            assert result["success"] is True
            balance.deduct_credits.assert_awaited_once()
            want = "charged"
        else:
            with pytest.raises(HTTPException) as exc:
                await admin.resolve_billing_failure(request, 7, resolution)
            assert exc.value.status_code == 503
            want = "pending"  # 098 MO-07: refused before the claim — row untouched
        row = await conn.fetch_one("SELECT status FROM billing_failures WHERE id=7")
        assert row["status"] == want
    finally:
        await conn.close()
