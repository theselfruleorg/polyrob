"""POLYROB's buyer must be able to price POLYROB's seller.

This is the test that would have caught the bug. Every other x402 parser test
uses a hand-written challenge fixture, so the parser and the fixtures agreed
with each other while disagreeing with the actual server. Here the challenge
comes from the REAL emitter — modules.x402.middleware.build_x402_challenge, the
same function api/payment_verification.py serves — and is fed to the REAL
buyer-side parser, in the same shape the middleware puts on the wire:
a JSON body, with no PAYMENT-REQUIRED header.
"""
import json

import pytest


class _MiddlewareShapedResponse:
    """Exactly what modules/x402/middleware.py returns: JSONResponse(402, body).

    Note the empty headers — the middleware sets no PAYMENT-REQUIRED header, and
    nothing else in the codebase does either.
    """

    def __init__(self, challenge: dict):
        self.status_code = 402
        self.headers = {}
        self.text = json.dumps(challenge)


@pytest.fixture
def real_challenge(monkeypatch):
    from modules.x402 import middleware

    monkeypatch.setattr(middleware, "get_x402_price_usd", lambda: 0.05, raising=False)
    monkeypatch.setattr(middleware, "get_x402_config",
                        lambda: {"network": "base-sepolia", "pay_to": "0xTreasury"},
                        raising=False)
    return middleware.build_x402_challenge("/a2a/rpc")


def test_our_own_challenge_carries_no_payment_required_header(real_challenge):
    """Pin the premise: the requirements live in the body, not a header."""
    assert "accepts" in real_challenge
    assert real_challenge["accepts"], "the emitter must offer at least one requirement"


def test_buyer_prices_our_own_seller(real_challenge):
    from tools.x402.real_client import RealX402Client

    parsed = RealX402Client._parse_402_challenge(_MiddlewareShapedResponse(real_challenge))

    assert parsed is not None, "the buyer could not see its own seller's challenge"
    assert parsed["amount"] == pytest.approx(0.05)
    assert parsed["pay_to"]


@pytest.mark.asyncio
async def test_probe_scores_our_own_seller_as_payable(real_challenge):
    """End to end: the emitter's bytes in, a payability verdict out."""
    from tools.x402.discovery import probe_endpoint

    async def _fetch(url, **kwargs):
        return _MiddlewareShapedResponse(real_challenge)

    class _AllowAll:
        def validate_and_resolve(self, url):
            return True, None, None

    row = await probe_endpoint("https://rob.test/a2a/rpc",
                               fetch=_fetch, validator=_AllowAll())

    assert row["status"] == 402
    assert row["challenge_parseable"] is True
    assert row["price_usd"] == pytest.approx(0.05)

    # The score must reflect what the emitter ACTUALLY put on the wire. When
    # fastapi_x402 is installed the challenge names the USDC contract and the
    # endpoint is payable outright; without it build_x402_challenge falls back
    # to asset=None (the import is wrapped in try/except), so the honest verdict
    # is "priced but not fully routed" — never a false 5/5.
    emitted_asset = real_challenge["accepts"][0].get("asset")
    if emitted_asset:
        assert row["score"] == 5, f"our own endpoint scored unpayable: {row['score_reasons']}"
    else:
        assert row["score"] == 4
        assert any("routing" in r for r in row["score_reasons"])
