"""F12: a GET at the advertised A2A URL answers, and a read-only probe prices
the POST-only A2A paywall.

The agent card advertises the JSON-RPC endpoint (`/a2a/rpc`); a GET there (and
at `/a2a`) used to answer 404. Now: x402 off -> 200 naming the method; x402 on
-> 402 whose challenge prices the POST (`outputSchema.input.method`); a GET
carrying a payment -> 405 (a GET is never billed).
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.a2a.endpoints import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def x402_off(monkeypatch):
    import modules.x402.x402_integration as integ
    monkeypatch.setattr(integ, "get_x402_config",
                        lambda: {"enabled": False, "pay_to": None, "network": "base"})


@pytest.fixture
def x402_on(monkeypatch):
    import modules.x402.middleware as mw
    import modules.x402.x402_integration as integ
    cfg = {"enabled": True, "pay_to": "0x" + "a" * 40, "network": "base-sepolia"}
    monkeypatch.setattr(integ, "get_x402_config", lambda: cfg)
    monkeypatch.setattr(mw, "get_x402_config", lambda: cfg)
    monkeypatch.setattr(mw, "get_x402_price_usd", lambda: 0.05)


@pytest.mark.parametrize("path", ["/a2a", "/a2a/rpc"])
def test_get_without_x402_names_the_post(x402_off, monkeypatch, path):
    monkeypatch.setenv("A2A_BASE_URL", "https://agent.example")
    r = _client().get(path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "POST"
    assert body["url"] == "https://agent.example/a2a/rpc"
    assert body["agent_card"] == "https://agent.example/.well-known/agent.json"


@pytest.mark.parametrize("path", ["/a2a", "/a2a/rpc"])
def test_get_with_x402_prices_the_post(x402_on, path):
    r = _client().get(path)
    assert r.status_code == 402, r.text
    accepts = r.json()["accepts"]
    assert accepts and all(a["outputSchema"]["input"]["method"] == "POST" for a in accepts)
    assert accepts[0]["resource"] == "/a2a/rpc"


def test_a_paid_get_is_refused_never_billed(x402_on):
    r = _client().get("/a2a/rpc", headers={"X-PAYMENT": "e30="})
    assert r.status_code == 405
    assert "POST" in r.json()["error"]


def test_the_probe_prices_the_post_only_paywall(x402_on):
    """The real seller response, fed to the real read-only probe."""
    from tools.x402.discovery import probe_endpoint
    from tools.x402.service import _render_probe

    resp = _client().get("/a2a/rpc")

    class _Resp:
        status_code = resp.status_code
        headers = {}
        text = resp.text

    async def _fetch(url, *, method="GET", body=None, timeout=None):
        return _Resp()

    class _V:
        def validate_and_resolve(self, url):
            return True, None, None

    row = asyncio.run(probe_endpoint("https://agent.example/a2a/rpc",
                                     fetch=_fetch, validator=_V()))
    assert row["status"] == 402
    assert row["price_usd"] == pytest.approx(0.05)
    assert row["pay_method"] == "POST"
    assert "pay with  : POST" in _render_probe(row)


def test_the_card_advertises_the_json_rpc_endpoint(monkeypatch):
    from api.a2a.agent_card import build_agent_card
    monkeypatch.setenv("A2A_BASE_URL", "https://agent.example")
    assert build_agent_card().url == "https://agent.example/a2a/rpc"
