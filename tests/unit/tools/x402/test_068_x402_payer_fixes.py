"""068 W1: the x402 payer faults that made a real payment fail.

X1 — the SDK's own default spend control ($1 per payment) was never set, so any
     payment above $1 was refused even after the owner approved it.
X3 — the durable replay key was `x402:{url}:{cap}`: a metered URL payable once, ever.
X5 — only `accepts[0]` was read: a server listing another network first was
     refused although a Base-USDC entry was offered.
"""
import base64
import inspect
import json

from tools.x402.real_client import RealX402Client as C
from tools.x402.service import FetchParams, x402_idempotency_key

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POLYGON_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"


class _Resp:
    def __init__(self, challenge):
        self.status_code = 402
        self.headers = {"PAYMENT-REQUIRED": base64.b64encode(
            json.dumps(challenge).encode()).decode()}
        self.text = ""


def _challenge(*entries):
    return {"x402Version": 2, "accepts": list(entries)}


def _entry(network, asset, amount):
    return {"scheme": "exact", "network": network, "asset": asset, "amount": amount,
            "payTo": "0x1111111111111111111111111111111111111111",
            "maxTimeoutSeconds": 60}


# ---- X5 ---------------------------------------------------------------------

def test_the_payable_entry_is_read_even_when_it_is_not_first():
    resp = _Resp(_challenge(_entry("eip155:137", POLYGON_USDC, "9000000"),
                            _entry("eip155:8453", BASE_USDC, "50000")))
    out = C._parse_402_challenge(resp, configured="mainnet")
    assert out["network"] == "eip155:8453" and out["asset"] == BASE_USDC
    assert abs(out["amount"] - 0.05) < 1e-9


def test_without_a_payable_entry_the_first_stays_and_is_refused_later():
    resp = _Resp(_challenge(_entry("eip155:137", POLYGON_USDC, "50000")))
    out = C._parse_402_challenge(resp, configured="mainnet")
    assert out["network"] == "eip155:137"


def test_unconfigured_parse_is_unchanged():
    resp = _Resp(_challenge(_entry("eip155:137", POLYGON_USDC, "9000000"),
                            _entry("eip155:8453", BASE_USDC, "50000")))
    assert C._parse_402_challenge(resp)["network"] == "eip155:137"


# ---- X1 ---------------------------------------------------------------------

def test_the_sdk_spend_control_is_set_to_our_cap_and_payable_first():
    src = inspect.getsource(C.fetch_with_payment)
    assert "set_spend_controls" in src
    assert src.index("set_spend_controls") < src.index("register_exact_evm_client(x402_c")
    assert "_payable_first" in src


def test_the_installed_sdk_parses_the_cap_we_pass():
    from x402.client_base import parse_money
    assert parse_money(f"${25.0:.6f}")["amount"] == "25.000000"


# ---- X3 ---------------------------------------------------------------------

def _p(**kw):
    base = {"url": "https://api.example/v1/data", "max_amount_usd": 0.1}
    base.update(kw)
    return FetchParams(**base)


def test_plain_get_keeps_the_old_key():
    assert x402_idempotency_key(_p()) == "x402:https://api.example/v1/data:0.1"


def test_a_retry_maps_to_the_same_key():
    assert x402_idempotency_key(_p(request_id="q1")) == x402_idempotency_key(_p(request_id="q1"))


def test_deliberate_calls_get_distinct_keys():
    keys = {x402_idempotency_key(_p(request_id="q1")),
            x402_idempotency_key(_p(request_id="q2")),
            x402_idempotency_key(_p(method="POST", body='{"a":1}')),
            x402_idempotency_key(_p(method="POST", body='{"a":2}'))}
    assert len(keys) == 4


# ---- Codex B8: quote, preflight and payer choose the same entry ----------------

import pytest  # noqa: E402

BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


@pytest.mark.asyncio
async def test_service_pays_the_cheap_base_entry_listed_second(monkeypatch):
    import httpx
    from tests.unit.tools.x402.test_service import _wallet
    from tools.x402.client import X402Result
    from tools.x402.service import X402PayTool

    challenge = _challenge(_entry("eip155:137", POLYGON_USDC, "9000000"),
                           _entry("eip155:84532", BASE_SEPOLIA_USDC, "50000"))

    class _Http:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp(challenge)
    monkeypatch.setattr(httpx, "AsyncClient", _Http)

    seen = {}

    class _Client(C):
        async def fetch_with_payment(self, **kw):
            seen.update(kw)
            return X402Result(body="DATA", paid=True, amount_usd=0.05, tx_hash="0xf",
                              pay_to="0x1", status_code=200)

    tool = X402PayTool(wallet=_wallet(), client=_Client())
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=0.05,
                                            request_id="q1"))
    assert res.error is None, res.error
    assert seen["idempotency_key"].endswith(":rid=q1")   # B6: the key reaches the journal row


def test_quote_and_preflight_share_the_payer_selection():
    src = inspect.getsource(C.quote_details)
    assert "configured=network" in src
    assert "network=network" in inspect.getsource(C.quote)
