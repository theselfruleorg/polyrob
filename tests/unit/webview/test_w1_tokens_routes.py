"""W1: Money › Book — the tokens Rob trusts, and the owner's word on one.

The console reaches the SAME ``core.wallet.token_trust`` functions the
Telegram / REPL verbs call; only the wallet owner may read or write.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import open_positions as op
from core.wallet import token_pins

REAL = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
FAKE = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import webview.pages as pages
    import webview.tokens_routes as routes
    monkeypatch.setattr(pages, "_effective_user_id", lambda req: "rob")
    monkeypatch.setattr(pages, "_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("core.wallet.authority.owner_refusal", lambda uid: None)
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    op.apply_delta("rob", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="PNL", qty=5.0, cost_usd=134.54),
                   db_path=op.open_positions_db_path(str(tmp_path)))
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_the_view_lists_sources_and_holdings(client):
    body = client.get("/api/webgate/tokens").json()
    assert body["user_id"] == "rob"
    assert isinstance(body["trusted"], list) and body["unreadable"] == []


def test_trust_then_untrust_then_writeoff(client, tmp_path):
    r = client.post("/api/webgate/tokens/trust",
                    json={"chain": "robinhood", "address": REAL, "symbol": "PNL"}).json()
    assert r["ok"], r
    assert token_pins.owner_pin("robinhood", REAL)["source"] == "owner_approved"
    r = client.post("/api/webgate/tokens/untrust",
                    json={"chain": "robinhood", "address": FAKE}).json()
    assert r["ok"] and token_pins.rejection("robinhood", FAKE)
    body = client.get("/api/webgate/tokens").json()
    assert body["holdings"][0]["status"] == "quarantined"
    r = client.post("/api/webgate/tokens/writeoff",
                    json={"chain": "robinhood", "address": FAKE, "reason": "rug"}).json()
    assert r["ok"], r
    pos = op.get_position("rob", "robinhood", FAKE,
                          db_path=op.open_positions_db_path(str(tmp_path)))
    assert pos.status == "written_off"


def test_bad_input_is_refused(client):
    assert client.post("/api/webgate/tokens/steal", json={}).status_code == 404
    assert client.post("/api/webgate/tokens/trust", json={}).status_code == 400


def test_a_non_owner_is_refused(client, monkeypatch):
    monkeypatch.setattr("core.wallet.authority.owner_refusal", lambda uid: "not the owner")
    assert client.get("/api/webgate/tokens").status_code == 403
    assert client.post("/api/webgate/tokens/trust",
                       json={"chain": "robinhood", "address": REAL}).status_code == 403
