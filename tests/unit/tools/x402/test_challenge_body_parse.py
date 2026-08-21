"""A 402 challenge carried in the RESPONSE BODY must be parsed, not ignored.

POLYROB's OWN seller emits the challenge as a JSON body with NO
``PAYMENT-REQUIRED`` header (``modules/x402/middleware.py`` returns
``JSONResponse(status_code=402, content=build_x402_challenge(path))``), and the
x402 reference servers do the same. The client parser read ONLY the header, so
``quote()`` returned None for that whole class of servers and the agent was told
"is not a paid resource (no x402 challenge)" about an endpoint that very much
charges — including POLYROB's own gated routes.

Header-carried challenges keep working exactly as before; the body is a
fallback consulted only when the header is absent.
"""
import base64
import json

import pytest


class _Resp:
    """Minimal httpx-Response stand-in: header challenge, body challenge, or both."""

    def __init__(self, status_code=402, header_challenge=None, body=None):
        self.status_code = status_code
        self.headers = {}
        if header_challenge is not None:
            enc = base64.b64encode(json.dumps(header_challenge).encode()).decode()
            self.headers["PAYMENT-REQUIRED"] = enc
        self.text = body if isinstance(body, str) else (
            json.dumps(body) if body is not None else "")


POLYROB_OWN_CHALLENGE = {
    "x402Version": 1,
    "accepts": [{
        "scheme": "exact",
        "network": "base",
        "maxAmountRequired": "10000",
        "resource": "/a2a/rpc",
        "payTo": "0xTreasury",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }],
}


def test_body_challenge_is_priced_when_no_header():
    """The regression: POLYROB's own 402 shape used to parse as None."""
    from tools.x402.real_client import RealX402Client as C

    out = C._parse_402_challenge(_Resp(402, body=POLYROB_OWN_CHALLENGE))

    assert out is not None, "body-carried challenge parsed as 'not a paid resource'"
    assert out["amount"] == pytest.approx(0.01)  # 10000 / 1e6 USDC
    assert out["network"] == "base"
    assert out["pay_to"] == "0xTreasury"


def test_quote_amount_from_body_challenge():
    from tools.x402.real_client import RealX402Client as C

    assert C._parse_402_amount(_Resp(402, body=POLYROB_OWN_CHALLENGE)) == pytest.approx(0.01)


def test_v2_body_challenge_uses_the_amount_field():
    from tools.x402.real_client import RealX402Client as C

    ch = {"x402Version": 2,
          "accepts": [{"amount": "50000", "network": "eip155:8453", "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, body=ch))

    assert out["amount"] == pytest.approx(0.05)
    assert out["network"] == "eip155:8453"


def test_header_still_wins_when_both_are_present():
    """The header is the pre-existing contract — the body is only a fallback."""
    from tools.x402.real_client import RealX402Client as C

    header_ch = {"accepts": [{"maxAmountRequired": "100000", "network": "base-sepolia",
                              "payTo": "0xheader"}]}
    body_ch = {"accepts": [{"maxAmountRequired": "999999", "network": "base",
                            "payTo": "0xbody"}]}
    out = C._parse_402_challenge(_Resp(402, header_challenge=header_ch, body=body_ch))

    assert out["pay_to"] == "0xheader"
    assert out["amount"] == pytest.approx(0.1)


def test_nested_error_accepts_shape_is_parsed():
    """Some servers nest the requirements under `error` — seen in the wild."""
    from tools.x402.real_client import RealX402Client as C

    ch = {"error": {"accepts": [{"maxAmountRequired": "25000", "network": "base",
                                 "payTo": "0xnested"}]}}
    out = C._parse_402_challenge(_Resp(402, body=ch))

    assert out["amount"] == pytest.approx(0.025)
    assert out["pay_to"] == "0xnested"


def test_legacy_singular_accept_is_parsed():
    from tools.x402.real_client import RealX402Client as C

    ch = {"accept": {"maxAmountRequired": "1000", "network": "base", "payTo": "0xsing"}}
    out = C._parse_402_challenge(_Resp(402, body=ch))

    assert out["amount"] == pytest.approx(0.001)
    assert out["pay_to"] == "0xsing"


def test_json_embedded_in_prose_body_is_recovered():
    from tools.x402.real_client import RealX402Client as C

    body = 'Payment required. {"accepts": [{"maxAmountRequired": "2000", ' \
           '"network": "base", "payTo": "0xprose"}]} Please pay.'
    out = C._parse_402_challenge(_Resp(402, body=body))

    assert out["amount"] == pytest.approx(0.002)


def test_no_header_and_no_body_is_still_none():
    """A genuinely free resource must stay None — never a fake $0 challenge."""
    from tools.x402.real_client import RealX402Client as C

    assert C._parse_402_challenge(_Resp(200)) is None


def test_unparseable_body_is_marked_not_free():
    """Present-but-broken must fail CLOSED, exactly like the header path."""
    from tools.x402.real_client import RealX402Client as C

    out = C._parse_402_challenge(_Resp(402, body="totally not json {{{"))

    assert out is not None
    assert out["amount"] is None
    assert out.get("_unparseable")


def test_response_without_a_text_attribute_does_not_explode():
    """Old call sites / test doubles pass objects with headers only."""
    from tools.x402.real_client import RealX402Client as C

    class HeadersOnly:
        status_code = 402
        headers = {}

    assert C._parse_402_challenge(HeadersOnly()) is None
