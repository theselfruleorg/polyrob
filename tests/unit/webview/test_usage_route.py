"""113 W6 item 4 (G-3): AI cost in the console — ``GET /api/webgate/usage``.

The route is the ``/usage`` verb's view (``core.status_economics.spend_view``) and
its one render (``spend_view_lines``); tenant-scoped; an unreadable record is
``readable: false`` with the reason, never a zero.
"""
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user="u1"):
    import webview.pages as pages
    import webview.usage_routes as mod
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.delenv("DB_PATH", raising=False)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def test_no_records_yet_is_said_not_zeroed(monkeypatch, tmp_path):
    body = _client(monkeypatch, tmp_path).get("/api/webgate/usage").json()
    assert body["readable"] is True and body["no_ledger"] is True and body["days"] == 7
    assert body["lines"] == ["AI cost, the last 7 days: no records yet (no AI request was made)."]


def test_the_lines_are_the_verbs_lines(monkeypatch, tmp_path):
    import time
    from core.status_economics import spend_view, spend_view_lines
    calls = []

    def fake_view(user_id, data_dir, *, now, days):
        calls.append((user_id, data_dir, days))
        return {"economics": {"calls": 0, "cost_usd": 0.0}, "runs_error": None,
                "no_ledger": False, "days": days}
    monkeypatch.setattr("core.status_economics.spend_view", fake_view)
    body = _client(monkeypatch, tmp_path, user="alice").get("/api/webgate/usage?days=30").json()
    assert calls == [("alice", str(tmp_path), 30)]                  # the caller's tenant
    assert body["lines"] == spend_view_lines(fake_view("x", "y", now=time.time(), days=30))


def test_an_unreadable_record_is_not_a_zero(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise sqlite3.DatabaseError("file is not a database")
    monkeypatch.setattr("core.status_economics.spend_view", boom)
    body = _client(monkeypatch, tmp_path).get("/api/webgate/usage").json()
    assert body["readable"] is False and "DatabaseError" in body["error"]
    assert "lines" not in body


def test_a_bad_period_is_refused(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/webgate/usage?days=abc").status_code == 400
    assert client.get("/api/webgate/usage?days=0").status_code == 400
    assert client.get("/api/webgate/usage?days=500").json()["days"] == 90


def test_a_caller_with_no_tenant_is_refused(monkeypatch, tmp_path):
    import webview.pages as pages
    client = _client(monkeypatch, tmp_path)

    def refuse(req):
        raise HTTPException(status_code=403, detail="no identity")
    monkeypatch.setattr(pages, "_effective_user_id", refuse)
    assert client.get("/api/webgate/usage").status_code == 403


def test_it_is_a_money_contribution():
    from webview.contributions import contributed_routers
    import webview.usage_routes as mod
    assert mod.router in contributed_routers("money")
