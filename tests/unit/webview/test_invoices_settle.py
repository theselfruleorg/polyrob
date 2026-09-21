"""030 WS-C C3 — webview invoice listing + settle over the x402 seam.

GET /api/webgate/invoices reuses ``modules.x402.invoicing.list_payment_requests``
(tenant-scoped exactly like `polyrob owner invoices --user` / REPL `/invoices`);
POST /api/webgate/invoices/{id}/settle mirrors `polyrob owner settle` with
Telegram `/settle`'s tenant scoping (the id resolves among the CALLER's own
pending rows only). Seeded tmp bot.db — the same fixture shape as
tests/unit/cli/test_repl_owner_verbs.py.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path, user_id="u1"):
    import webview.pages as pages
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: user_id)
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    return TestClient(_app(pages)), pages


def _app(pages):
    app = FastAPI()
    app.include_router(pages.router)
    return app


@pytest.fixture()
def _invoice_db(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_INVOICE_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    db_path = tmp_path / "bot.db"

    async def setup():
        from modules.database.connection import DatabaseConnection
        from modules.database.user_profiles import UserProfiles
        from modules.database.x402_tables import X402Tables
        from modules.x402 import invoicing

        db = DatabaseConnection(db_path)
        await db.connect()
        try:
            await UserProfiles(db).create_table()
            await X402Tables(db).create_tables()
            inv = await invoicing.create_payment_request(
                user_id="u1", session_id="s1", amount_usd=5.0,
                purpose="widget", payer_contact="Alice <a@x.com>", db=db)
        finally:
            await db.close()
        return inv

    inv = asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Force the fallback bot.db resolution (the deployed webview shape): a
    # container singleton left over from another test must not shadow it.
    from core.container import DependencyContainer
    monkeypatch.setattr(DependencyContainer, "_instance", None)
    return inv


def test_invoices_lists_seeded_row_tenant_scoped(monkeypatch, tmp_path, _invoice_db):
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    body = client.get("/api/webgate/invoices").json()
    assert body["count"] == 1
    row = body["invoices"][0]
    assert row["purpose"] == "widget"
    assert row["status"] == "pending"
    assert row["payer_contact"] == "Alice <a@x.com>"
    # the honest feature-off note (X402_INVOICE_ENABLED unset -> watcher off)
    assert "X402_INVOICE_ENABLED" in (body["note"] or "")

    other, _ = _client(monkeypatch, tmp_path, user_id="someone-else")
    assert other.get("/api/webgate/invoices").json()["count"] == 0


def test_invoices_rejects_unknown_status(monkeypatch, tmp_path, _invoice_db):
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    assert client.get("/api/webgate/invoices",
                      params={"status": "bogus"}).status_code == 400


def test_settle_happy_path(monkeypatch, tmp_path, _invoice_db):
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    request_id = _invoice_db["request_id"]
    r = client.post(f"/api/webgate/invoices/{request_id}/settle")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["request_id"] == request_id
    # the row actually completed
    listed = client.get("/api/webgate/invoices",
                        params={"status": "completed"}).json()
    assert [i["request_id"] for i in listed["invoices"]] == [request_id]


def test_settle_unknown_id_404(monkeypatch, tmp_path, _invoice_db):
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    r = client.post("/api/webgate/invoices/not-a-real-id/settle")
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_settle_is_tenant_scoped(monkeypatch, tmp_path, _invoice_db):
    """Another tenant cannot attest u1's invoice — the id resolves only among
    the CALLER's own pending rows (Telegram /settle semantics)."""
    client, _ = _client(monkeypatch, tmp_path, user_id="someone-else")
    request_id = _invoice_db["request_id"]
    r = client.post(f"/api/webgate/invoices/{request_id}/settle")
    assert r.status_code == 404
    # the row is untouched
    owner, _ = _client(monkeypatch, tmp_path, user_id="u1")
    pending = owner.get("/api/webgate/invoices",
                        params={"status": "pending"}).json()
    assert pending["count"] == 1


def test_settle_read_only_refused_before_db(monkeypatch, tmp_path):
    """Read-only refusal fires before any DB access (no bot.db needed)."""
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    assert client.post("/api/webgate/invoices/x/settle").status_code == 403


def test_invoices_without_db_is_honest_not_empty(monkeypatch, tmp_path):
    """No bot.db anywhere -> the listing carries an error field, never a
    silent 'no invoices' (030 D4)."""
    from core.container import DependencyContainer
    monkeypatch.setattr(DependencyContainer, "_instance", None)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)  # no data-home bot.db candidates here
    client, _ = _client(monkeypatch, tmp_path, user_id="u1")
    body = client.get("/api/webgate/invoices").json()
    # 043 A35: an unreadable ledger is NULL, not zero — and the outstanding
    # total it could not compute is null too, never $0.00.
    assert body["count"] is None
    assert body["invoices"] is None
    assert body["outstanding_usd_total"] is None
    assert body["outstanding_count"] is None
    assert body.get("error")
