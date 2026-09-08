"""M1 (security audit 2026-08-22): modules/x402/middleware.py:426 is a SECOND
store site for the same x402_payment_requests.transaction_hash column that
modules.x402.invoicing normalizes (a machine-payer x402 settlement, e.g. a
paid A2A/OpenAI-compat request -- not an agent invoice, but the SAME column
and the SAME partial unique index). If this call kept storing the
facilitator's raw-case hash, the on-chain scanner (always lowercase,
onchain_probe.py) could never find this row via
transaction_hash_already_settled -- the exact "normalize one side only"
trap called out in the task brief.

The real fastapi_x402 package is not installed in this environment (no
existing test in this suite exercises X402PaymentMiddleware's settle path
either -- test_402_challenge.py only reaches the pre-payment 402-challenge
branch, which degrades gracefully without the package). This test stubs the
narrow fastapi_x402 import surface middleware.py's settle path actually
touches (PaymentRequirements + the two network-config lookups) via
sys.modules, then exercises the REAL middleware.dispatch() end-to-end --
nothing about middleware.py's own logic, including the line-426 store call
under test, is mocked.
"""
import base64
import json
import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth_state import set_auth_state
from modules.x402 import middleware as mw_mod
from modules.x402.middleware import X402PaymentMiddleware, install_auth_state_writer


class _FakeAssetConfig:
    decimals = 6
    address = "0xUSDC"
    eip712_name = "USDC"
    eip712_version = "2"


class _FakeVerifyResponse:
    isValid = True
    error = None


class _FakeSettleResponse:
    def __init__(self, tx):
        self.success = True
        self.errorReason = None
        self.transaction = tx


class _FakeFacilitator:
    """Stands in for fastapi_x402's real facilitator client -- returns a
    MIXED-CASE settlement hash, exactly what a real facilitator is free to
    do (settle_resp.transaction is stored verbatim upstream of this fix)."""

    def __init__(self, tx):
        self._tx = tx

    async def verify_and_settle_payment(self, *, payment_header, payment_requirements):
        return _FakeVerifyResponse(), _FakeSettleResponse(self._tx)


def _install_fake_fastapi_x402(monkeypatch):
    models_mod = types.ModuleType("fastapi_x402.models")

    class PaymentRequirements:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    models_mod.PaymentRequirements = PaymentRequirements

    networks_mod = types.ModuleType("fastapi_x402.networks")
    networks_mod.get_default_asset_config = lambda network: _FakeAssetConfig()
    networks_mod.get_network_config = lambda network: object()  # fetched, never used downstream

    pkg = types.ModuleType("fastapi_x402")
    pkg.models = models_mod
    pkg.networks = networks_mod

    monkeypatch.setitem(sys.modules, "fastapi_x402", pkg)
    monkeypatch.setitem(sys.modules, "fastapi_x402.models", models_mod)
    monkeypatch.setitem(sys.modules, "fastapi_x402.networks", networks_mod)


def _payment_header(payer="0xPayerAddress000000000000000000000001"):
    payload = {"payload": {"authorization": {"from": payer}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _client(monkeypatch, mixed_case_tx, captured):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "1" * 40)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    _install_fake_fastapi_x402(monkeypatch)
    install_auth_state_writer(set_auth_state)

    async def _fake_record(**kwargs):
        captured.update(kwargs)
        return True

    async def _fake_ensure_profile(*a, **kw):
        return True

    monkeypatch.setattr(mw_mod, "record_x402_payment", _fake_record)
    monkeypatch.setattr(mw_mod, "ensure_user_profile_for_payer", _fake_ensure_profile)
    # Bypass the real facilitator bootstrap (network calls / SDK config) --
    # inject the fake facilitator directly, mirroring how a real deployment
    # would have self._facilitator_client set after a successful init.
    monkeypatch.setattr(
        X402PaymentMiddleware, "_init_facilitator",
        lambda self: setattr(self, "_facilitator_client", _FakeFacilitator(mixed_case_tx)),
    )

    app = FastAPI()

    @app.post("/a2a/rpc")
    async def _rpc():
        return {"ok": True}

    app.add_middleware(X402PaymentMiddleware, enabled=True)
    return TestClient(app, raise_server_exceptions=False)


def test_middleware_normalizes_tx_hash_case_before_recording(monkeypatch):
    mixed_case_tx = "0xDEADBEEFCafeBabe00000000000000000000000000000000000000000123"
    captured = {}
    client = _client(monkeypatch, mixed_case_tx, captured)

    resp = client.post("/a2a/rpc", json={"x": 1}, headers={"X-PAYMENT": _payment_header()})

    assert resp.status_code == 200, resp.text
    assert captured.get("transaction_hash") == mixed_case_tx.lower()
    # actually normalized, not coincidentally already-lowercase input
    assert captured["transaction_hash"] != mixed_case_tx


def test_middleware_store_is_idempotent_on_already_lowercase_hash(monkeypatch):
    """Sanity: an already-lowercase facilitator hash (the common/expected
    case) passes through unchanged -- this fix must not corrupt the normal
    path."""
    already_lower_tx = "0xabc0000000000000000000000000000000000000000000000000000000ff"
    captured = {}
    client = _client(monkeypatch, already_lower_tx, captured)

    resp = client.post("/a2a/rpc", json={"x": 1}, headers={"X-PAYMENT": _payment_header()})

    assert resp.status_code == 200, resp.text
    assert captured.get("transaction_hash") == already_lower_tx
