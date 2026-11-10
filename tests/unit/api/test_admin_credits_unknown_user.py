"""The admin credit routes answer 404 for an unknown user id before they touch
the balance store (a GET used to INSERT a user_credits row; with foreign keys ON
that was a 500). /admin/users bounds limit and offset like its siblings."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import admin_endpoints as admin


@pytest.fixture
def client(monkeypatch):
    from core.container import DependencyContainer
    db = SimpleNamespace(fetch_one=AsyncMock(return_value=None), fetch_all=AsyncMock(return_value=[]))
    balance = SimpleNamespace(get_balance=AsyncMock(return_value={"balance": 0}),
                              add_credits=AsyncMock(return_value=True),
                              deduct_credits=AsyncMock(return_value=True),
                              get_transaction_history=AsyncMock(return_value=[]),
                              get_monthly_stats=AsyncMock(return_value={}))
    container = SimpleNamespace(get_service={"database_manager": db,
                                             "balance_manager": balance}.get)
    monkeypatch.setattr(DependencyContainer, "get_instance", lambda: container)
    monkeypatch.setattr(admin, "get_audit_logger", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    app.dependency_overrides[admin.require_admin] = lambda: True
    return TestClient(app, raise_server_exceptions=False), balance


@pytest.mark.parametrize("method,path", [
    ("get", "/api/admin/users/nobody/credits"),
    ("post", "/api/admin/users/nobody/credits/add"),
    ("post", "/api/admin/users/nobody/credits/deduct"),
])
def test_unknown_user_is_404_and_the_balance_store_is_untouched(client, method, path):
    http, balance = client
    kwargs = {"json": {"amount": 5, "reason": "fixture"}} if method == "post" else {}
    resp = getattr(http, method)(path, **kwargs)
    assert resp.status_code == 404, resp.text
    balance.get_balance.assert_not_awaited()
    balance.add_credits.assert_not_awaited()
    balance.deduct_credits.assert_not_awaited()


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
def test_list_users_bounds_limit_and_offset(client, query):
    http, _ = client
    assert http.get(f"/api/admin/users?{query}").status_code == 422
