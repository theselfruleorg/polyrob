"""A refused console settle names every reason, including an order's invoice
(it settles only with a transaction hash)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_the_refusal_names_the_order_invoice_reason(monkeypatch):
    import modules.x402.invoicing as inv
    import webview.pages as pages

    async def _list(**kw):
        return [{"request_id": "req-fixture-1"}]

    async def _settle(request_id, transaction_hash=None, db=None):
        return False

    async def _run(factory):
        return True, await factory(object())

    monkeypatch.setattr(inv, "list_payment_requests", _list)
    monkeypatch.setattr(inv, "settle_payment_request", _settle)
    monkeypatch.setattr(pages, "_with_owner_money_db", _run)
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "alice")
    app = FastAPI()
    app.include_router(pages.money_router)
    resp = TestClient(app).post("/api/webgate/invoices/req-fixture-1/settle", json={})
    assert resp.status_code == 409, resp.text
    msg = resp.json()["message"]
    assert "already settled another invoice" in msg
    assert "or it is an order's invoice: give the transaction hash" in msg
