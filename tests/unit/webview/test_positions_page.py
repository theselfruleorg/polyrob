"""/positions — the on-chain book panel (2026-08-27 crypto finalization).

The console showed money totals but never the positions behind them; this is
the UI counterpart of the position-recall incident. Contract: the JSON endpoint
reuses the read verbs (portfolio/reconcile, mocked at the tool seam), honors
DEFI_DATA_ENABLED at access time, and degrades to LABELED unavailability —
never a 500, never a fabricated empty book.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    import webview.pages as pages
    app = FastAPI()
    app.include_router(pages.router)
    return TestClient(app), pages


class _AR:
    def __init__(self, content=None, error=None):
        self.extracted_content = content
        self.error = error


class _FakeTool:
    def __init__(self, *a, **k):
        pass

    async def portfolio(self, params, execution_context=None):
        return _AR(content=f"holdings for 0xME (chain {params.chain})")

    async def reconcile(self, params, execution_context=None):
        return _AR(content="VERDICT: DISAGREEMENT — 3 unexplained holdings")


def test_positions_off_is_labeled_not_500(monkeypatch):
    monkeypatch.delenv("DEFI_DATA_ENABLED", raising=False)
    client, _ = _client()
    res = client.get("/api/webgate/positions")
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is False
    assert "DEFI_DATA_ENABLED" in (data["error"] or "")
    assert data["portfolio"] is None and data["reconcile"] is None


def test_positions_renders_portfolio_and_reconcile(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")
    import tools.defi.data_tool as dt
    monkeypatch.setattr(dt, "DefiDataTool", _FakeTool)
    ledger = tmp_path / "ledger.md"
    ledger.write_text("## Open positions\n")
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(ledger))
    client, _ = _client()
    res = client.get("/api/webgate/positions?chain=base")
    assert res.status_code == 200
    data = res.json()
    assert data["portfolio"]["text"].startswith("holdings for")
    assert "DISAGREEMENT" in data["reconcile"]["text"]
    assert data["ledger_path"] == str(ledger)


def test_positions_tool_error_is_carried_not_hidden(monkeypatch):
    monkeypatch.setenv("DEFI_DATA_ENABLED", "true")

    class _ErrTool(_FakeTool):
        async def portfolio(self, params, execution_context=None):
            return _AR(error="agent wallet not enabled")

    import tools.defi.data_tool as dt
    monkeypatch.setattr(dt, "DefiDataTool", _ErrTool)
    client, _ = _client()
    res = client.get("/api/webgate/positions")
    data = res.json()
    assert data["portfolio"]["error"] == "agent wallet not enabled"
    assert res.status_code == 200
