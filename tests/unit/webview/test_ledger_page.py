"""Finance dashboard — /finance page + /api/webgate/ledger JSON over build_ledger.

The JSON endpoint REUSES ``modules.credits.unified_ledger.build_ledger`` (mocked
at the ``webview.pages`` seam here) and is tenant-scoped via ``_effective_user_id``.
All-zero-tolerant: a ledger error degrades to zeros, never a 500.

Two ledgers that must NEVER be summed: ``treasury`` (the agent's own USDC) and
``runtime`` (the owner's API bill) — see ``modules.credits.unified_ledger``.
This is a DISPLAY surface, so ``api_ledger`` passes ``include_balances=True``.
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


def _split_ledger(user_id, days, **treasury_runtime_overrides):
    """Build a full split-shape ledger dict (legacy flat keys + treasury/runtime)."""
    treasury = {
        "income_usd": 5.0, "spend_usd": 2.0, "pending_usd": 3.0,
        "pending_count": 1, "balance_usd": None, "net_usd": 3.0,
        "available": True,
    }
    runtime = {
        "spend_window_usd": 2.0, "spend_total_usd": 2.0,
        "calls_window": 3, "calls_total": 3,
        "provider_balance_usd": None, "available": True,
    }
    treasury.update(treasury_runtime_overrides.get("treasury") or {})
    runtime.update(treasury_runtime_overrides.get("runtime") or {})
    return {
        "user_id": user_id, "window_days": days, "llm_api_cost_usd": 2.0,
        "credits_spent": 0.1, "llm_calls": 3, "wallet_spend_usd": 2.0,
        "wallet_payments": 1, "settled_payments": 1,
        "pending_invoices_usd": 3.0, "pending_invoices": 1,
        "treasury": treasury, "runtime": runtime,
    }


async def _fake_ledger(user_id, *, days=7, db=None, include_balances=False):
    return _split_ledger(user_id, days)


def _get_ledger(client, days=7):
    r = client.get(f"/api/webgate/ledger?days={days}")
    assert r.status_code == 200
    return r.json()


def test_ledger_api_returns_split_blocks(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "build_ledger", _fake_ledger)
    body = _get_ledger(client)
    # treasury.net_usd = income - spend (5.0 - 2.0), NEVER the merged figure
    # that also subtracts runtime cost.
    assert body["treasury"]["net_usd"] == 3.0
    assert body["runtime"]["spend_window_usd"] == 2.0
    assert body["treasury"]["income_usd"] == 5.0
    assert body["treasury"]["spend_usd"] == 2.0
    assert body["runtime"]["spend_total_usd"] == 2.0


def test_ledger_endpoint_passes_include_balances(monkeypatch):
    """Both CLI /finance and the webview Finance page are DISPLAY surfaces —
    api_ledger must opt into the balance probes."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    captured = {}

    async def _capture(user_id, *, days=7, db=None, include_balances=False):
        captured["include_balances"] = include_balances
        return _split_ledger(user_id, days)

    monkeypatch.setattr(pages, "build_ledger", _capture)
    _get_ledger(client)
    assert captured["include_balances"] is True


def test_ledger_endpoint_clamps_days(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    captured = {}

    async def _cap_ledger(user_id, *, days=7, db=None, include_balances=False):
        captured["days"] = days
        return _split_ledger(user_id, days)

    monkeypatch.setattr(pages, "build_ledger", _cap_ledger)
    client.get("/api/webgate/ledger?days=99999")
    assert captured["days"] == 365
    client.get("/api/webgate/ledger?days=0")
    assert captured["days"] == 1


def test_ledger_endpoint_all_zero_on_error(monkeypatch):
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")

    async def _boom(user_id, *, days=7, db=None, include_balances=False):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(pages, "build_ledger", _boom)
    r = client.get("/api/webgate/ledger?days=7")
    assert r.status_code == 200
    body = r.json()
    # The merged legacy fields are gone (Task 8, no alias) — a surviving
    # earned_usd/total_spend_usd/net_usd here would mean the error-path
    # fallback drifted from the real build_ledger shape.
    assert "earned_usd" not in body
    assert "total_spend_usd" not in body
    assert "net_usd" not in body            # top level; treasury.net_usd remains
    assert body["costs_available"] is False
    assert body["inbound_available"] is False
    assert body["wallet_metering"] == "error"
    # _empty_ledger must stay SHAPE-IDENTICAL to a real ledger on the error
    # path (same treasury/runtime blocks) — else the Finance page silently
    # renders a different shape than the happy path.
    assert body["treasury"] == {
        "income_usd": 0.0, "spend_usd": 0.0, "pending_usd": 0.0,
        "pending_count": 0, "balance_usd": None, "net_usd": 0.0,
        "available": False,
    }
    assert body["runtime"] == {
        "spend_window_usd": 0.0, "spend_total_usd": 0.0,
        "calls_window": 0, "calls_total": 0,
        "provider_balance_usd": None, "available": False,
    }


def test_ledger_endpoint_note_absent_when_fully_available(monkeypatch):
    """Final review Finding 1 (related root cause): treasury/runtime carry
    `available` markers with a dedicated test but ZERO production readers —
    finance.html never mentions note/degraded/available. A healthy ledger
    (both legs available) must add no noise: no `note` key at all."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "build_ledger", _fake_ledger)
    body = _get_ledger(client)
    assert body.get("note") is None


def test_ledger_endpoint_includes_availability_note_when_degraded(monkeypatch):
    """A partially-degraded ledger (one leg unreadable) must surface a `note`
    on the JSON — the same `ledger_availability_note` helper core/recap.py and
    cli/ui/commands/h_finance.py already use — so finance.html has something
    to render instead of silently dropping the honesty markers on the floor."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")

    async def _degraded(user_id, *, days=7, db=None, include_balances=False):
        led = _split_ledger(user_id, days)
        led["costs_available"] = False
        led["runtime"]["available"] = False
        return led

    monkeypatch.setattr(pages, "build_ledger", _degraded)
    body = _get_ledger(client)
    assert body.get("note") is not None
    assert "metering degraded" in body["note"].lower()


def test_ledger_endpoint_note_on_full_read_failure(monkeypatch):
    """The error-path fallback (_empty_ledger) is the webview's equivalent of
    the digest's ledger-read-{} case — both legs unavailable — and must also
    carry a `note` ("no data yet")."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")

    async def _boom(user_id, *, days=7, db=None, include_balances=False):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(pages, "build_ledger", _boom)
    body = _get_ledger(client)
    assert body.get("note") is not None
    assert "no data yet" in body["note"].lower()


def test_ledger_caps_drops_autonomy_budget(monkeypatch):
    """The autonomy-budget gate it described is deleted in a later task — the
    Finance page must not advertise a flag that no longer exists."""
    client, pages = _router_client()
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "u1")
    monkeypatch.setattr(pages, "build_ledger", _fake_ledger)
    body = _get_ledger(client)
    assert "autonomy_budget_usd" not in body["caps"]
    assert "invoice_max_usd" in body["caps"]


def test_finance_page_renders_200(monkeypatch):
    monkeypatch.setenv("WEBGATE_MULTITENANT", "false")
    monkeypatch.setenv("ENV", "development")
    import webview.server as server
    server = importlib.reload(server)
    client = TestClient(server._fastapi)
    r = client.get("/finance")
    assert r.status_code == 200
    assert "Finance" in r.text
    # terminology is income/spend — "earned" is retired everywhere, incl. blurbs.
    assert "earned" not in r.text.lower()


@pytest.fixture(autouse=True)
def _restore_server():
    yield
    import importlib
    import webview.server as server
    importlib.reload(server)


@pytest.mark.asyncio
async def test_empty_ledger_shape_matches_real_build_ledger():
    """The real guard: _empty_ledger's docstring claims it is SHAPE-IDENTICAL to
    a real build_ledger() result. Set-equality on the top-level keys (and the
    treasury/runtime sub-dicts) is what actually enforces that — it fails
    loudly the moment either side gains or loses a key, instead of silently
    drifting (as the top-level income_usd leak did)."""
    from modules.credits.unified_ledger import build_ledger
    from webview.pages import _empty_ledger
    from tests.unit.modules.credits.test_unified_ledger_split import FakeDB

    real = await build_ledger("rob", days=7, db=FakeDB())
    empty = _empty_ledger("rob", 7)

    assert set(real.keys()) == set(empty.keys())
    assert set(real["treasury"].keys()) == set(empty["treasury"].keys())
    assert set(real["runtime"].keys()) == set(empty["runtime"].keys())
