"""C2: one x402 price SSOT. The quoted 402-challenge price must equal the live
charge (get_x402_price_usd()), not a hardcoded 50% premium.
"""
from fastapi import Request

from api.payment_verification import payment_required_response
from modules.x402.x402_integration import get_x402_price_usd


class _FakeAppState:
    pass


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


def _fake_request():
    scope = {
        "type": "http", "method": "POST", "path": "/task/sessions",
        "headers": [], "query_string": b"",
        "app": _FakeApp(),
    }
    req = Request(scope)
    req.app.state.x402_handler = None
    return req


def test_402_challenge_price_matches_live_charge_default(monkeypatch):
    # C2 invariant: the quoted challenge price equals the live charge (no premium),
    # whatever get_x402_price_usd() resolves to (now economics-derived, not $0.01).
    monkeypatch.delenv("X402_PRICE_USD", raising=False)
    body = payment_required_response(_fake_request(), cost_credits=1)
    assert body["payment_options"]["x402"]["cost_usd"] == get_x402_price_usd()


def test_402_challenge_price_follows_env_override_not_premium(monkeypatch):
    # If the operator repriced x402 via env, the challenge must track it exactly —
    # no *1.5 on top.
    monkeypatch.setenv("X402_PRICE_USD", "0.05")
    body = payment_required_response(_fake_request(), cost_credits=1)
    assert body["payment_options"]["x402"]["cost_usd"] == 0.05


def test_extended_agent_card_never_overrides_x402_price(monkeypatch):
    import inspect
    import api.a2a.agent_card as agent_card
    src = inspect.getsource(agent_card)
    assert "tier == 'premium'" not in src, "dead premium-price override must stay removed"
