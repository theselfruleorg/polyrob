import importlib.util
import pytest

x402_installed = importlib.util.find_spec("x402") is not None


def test_real_client_class_importable():
    from tools.x402.real_client import RealX402Client
    assert RealX402Client is not None


@pytest.mark.skipif(not x402_installed, reason="x402 SDK not installed in this env")
def test_real_client_constructs():
    from tools.x402.real_client import RealX402Client
    RealX402Client()  # must not raise when the SDK is present


# --- pure-helper regressions (no SDK needed) -------------------------------

import base64 as _b64
import json as _json


class _Resp:
    def __init__(self, status_code=402, challenge=None):
        self.status_code = status_code
        self.headers = {}
        if challenge is not None:
            enc = _b64.b64encode(_json.dumps(challenge).encode()).decode()
            self.headers["PAYMENT-REQUIRED"] = enc


def test_parse_402_challenge_extracts_amount_network_payto():
    from tools.x402.real_client import RealX402Client as C
    ch = {"accepts": [{"maxAmountRequired": "100000", "network": "base-sepolia",
                       "payTo": "0xabc"}]}
    out = C._parse_402_challenge(_Resp(402, ch))
    assert out["amount"] == pytest.approx(0.1)  # 100000 / 1e6 USDC
    assert out["network"] == "base-sepolia"
    assert out["pay_to"] == "0xabc"


def test_parse_402_challenge_absent_header_is_none():
    from tools.x402.real_client import RealX402Client as C
    assert C._parse_402_challenge(_Resp(200)) is None


def test_parse_402_challenge_unparseable_marked_not_free():
    from tools.x402.real_client import RealX402Client as C
    r = _Resp(402)
    r.headers["PAYMENT-REQUIRED"] = "!!!not-base64!!!"
    out = C._parse_402_challenge(r)
    assert out is not None and out["amount"] is None and out.get("_unparseable")


def test_networks_match_loose_and_strict():
    from tools.x402.real_client import RealX402Client as C
    assert C._networks_match("base-sepolia", "base-sepolia") is True
    assert C._networks_match("base", "eip155:base-sepolia") is True  # substring
    assert C._networks_match("base-mainnet", "base-sepolia") is False
    assert C._networks_match("base", None) is True  # absent → don't block
