"""Finance dashboard — /finance page + /api/webgate/ledger JSON over build_ledger.

The JSON endpoint REUSES ``modules.credits.unified_ledger.build_ledger`` (mocked
at the ``webview.pages`` seam here) and is tenant-scoped via ``_effective_user_id``.
All-zero-tolerant: a ledger error degrades to zeros, never a 500.
"""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _router_client():
    import webview.pages as pages
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


async def _fake_ledger(user_id, *, days=7, db=None):
    return {"user_id": user_id, "window_days": days, "llm_api_cost_usd": 0.5,
            "credits_spent": 0.1, "llm_calls": 3, "wallet_spend_usd": 0.0,
            "wallet_payments": 0, "earned_usd": 2.0, "settled_payments": 1,
            "pending_invoices_usd": 3.0, "pending_invoices": 1,
            "total_spend_usd": 0.5, "net_usd": 1.5}


def test_ledger_endpoint_reuses_build_ledger(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "build_ledger", _fake_ledger)
    r = client.get("/api/webgate/ledger?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["net_usd"] == 1.5
    assert body["earned_usd"] == 2.0
    assert body["window_days"] == 7
    assert "caps" in body  # display-only policy caps attached


def test_ledger_endpoint_clamps_days(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    captured = {}

    async def _cap_ledger(user_id, *, days=7, db=None):
        captured["days"] = days
        return await _fake_ledger(user_id, days=days)

    monkeypatch.setattr(pages, "build_ledger", _cap_ledger)
    client.get("/api/webgate/ledger?days=99999")
    assert captured["days"] == 365
    client.get("/api/webgate/ledger?days=0")
    assert captured["days"] == 1


def test_ledger_endpoint_all_zero_on_error(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")

    async def _boom(user_id, *, days=7, db=None):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(pages, "build_ledger", _boom)
    r = client.get("/api/webgate/ledger?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["net_usd"] == 0.0 and body["earned_usd"] == 0.0
    assert body["total_spend_usd"] == 0.0


def test_finance_page_renders_200(monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    server = importlib.reload(server)
    client = TestClient(server._fastapi)
    r = client.get("/finance")
    assert r.status_code == 200
    assert "Finance" in r.text


@pytest.fixture(autouse=True)
def _restore_server():
    yield
    import importlib
    import webview.server as server
    importlib.reload(server)
