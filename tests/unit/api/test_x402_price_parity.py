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


def test_402_response_payment_details_nonempty_and_matches_middleware_shape(monkeypatch):
    """G-16: payment_required_response used to read `request.app.state.x402_handler`,
    which is never assigned anywhere (see api/app.py — x402 is handled entirely via
    the fastapi-x402 middleware, "no custom handler needed"). That always left
    `payment_details` as an empty dict. It must now share the same
    `build_x402_challenge` builder the middleware's own 402 uses, so a payer hitting
    EITHER 402 producer gets an equally usable `accepts` block.
    """
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "2" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_PRICE_USD", "0.03")

    body = payment_required_response(_fake_request(), cost_credits=1)
    x402_block = body["payment_options"]["x402"]

    assert x402_block["payment_details"], "payment_details must not be empty"
    assert x402_block["payment_details"]["payTo"] == "0x" + "2" * 40
    assert x402_block["payment_details"]["network"] == "base"
    assert x402_block["payment_details"]["maxAmountRequired"] == str(int(0.03 * 10 ** 6))

    # Same shape (accepts/x402Version) the middleware's build_x402_challenge produces.
    assert x402_block["accepts"][0] == x402_block["payment_details"]
    assert x402_block["x402Version"] == 1


def test_402_response_shares_middleware_challenge_builder():
    """The endpoint-layer 402 must not revive a dead app.state.x402_handler
    indirection — it should call the same builder the middleware uses."""
    import inspect
    import api.payment_verification as pv
    src = inspect.getsource(pv.payment_required_response)
    assert "getattr(request.app.state" not in src
    assert "build_x402_challenge" in src
