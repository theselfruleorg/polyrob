"""085 W4 — x402: book what we signed (X402OUT-1, x402out L2/L4).

A seller's PAYMENT-RESPONSE header is seller-written. EIP-3009 moves exactly the
signed ``value``, so the spend ledger books the signed amount; a header above it
is hostile and never reaches the ledger, the notice or the result text.
"""
import base64
import json

import pytest

from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import WalletConfig
from tools.x402.client import X402Result
from tools.x402.real_client import RealX402Client as C
from tools.x402.service import FetchParams, X402PayTool, x402_cap_charge


class _Resp:
    def __init__(self, headers):
        self.headers = headers


def _settle_header(amount):
    raw = json.dumps({"success": True, "transaction": "0xab",
                      "network": "eip155:8453", "amount": amount})
    return {"PAYMENT-RESPONSE": base64.b64encode(raw.encode()).decode()}


def _wallet():
    cfg = WalletConfig(enabled=True, backend="local_eoa", master_seed="s" * 40,
                       network="testnet", max_per_tx_usd=10.0,
                       x402_client_enabled=True, x402_facilitator_url="http://f")
    return AgentWallet(cfg)


class _ResultClient:
    def __init__(self, result):
        self._result = result

    async def quote(self, url, **kwargs):
        return 0.01

    async def fetch_with_payment(self, **kwargs):
        return self._result


# --- the review probe, as a test ---------------------------------------------

def test_1e24_settle_header_reconciles_to_the_signed_amount(caplog):
    pytest.importorskip("x402")
    settle = C._decode_settle_response(
        _Resp(_settle_header("1000000000000000000000000000000")))
    assert settle is not None and settle["amount"] == pytest.approx(1e24)
    payment_info = {"happened": True, "amount": 0.01, "authorized_amount": 0.01,
                    "pay_to": "0xR"}
    with caplog.at_level("ERROR"):
        paid, amount, estimate = C._reconcile_paid_amount(
            paid=True, settle=settle, payment_info=payment_info,
            probe_amount=0.01, max_amount_usd=1.0, url="http://fake/paid")
    assert paid is True
    assert amount == pytest.approx(0.01)
    assert estimate is True
    assert any("above the signed authorization" in r.message for r in caplog.records)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-5", "not-a-number"])
def test_non_finite_or_negative_settle_amount_is_none_never_a_raise(raw):
    pytest.importorskip("x402")
    settle = C._decode_settle_response(_Resp(_settle_header(raw)))
    # Either the SDK refuses the header (None) or the amount is dropped.
    assert settle is None or settle["amount"] is None


def test_reconcile_with_unusable_header_falls_back_to_signed_amount():
    settle = {"amount": None, "success": True, "tx_hash": "0xab"}
    paid, amount, estimate = C._reconcile_paid_amount(
        paid=True, settle=settle,
        payment_info={"happened": True, "amount": 0.02, "authorized_amount": 0.02},
        probe_amount=None, max_amount_usd=1.0, url="http://fake/paid")
    assert paid is True and amount == pytest.approx(0.02) and estimate is True


def test_reconcile_header_at_or_below_signed_amount_is_unchanged():
    settle = {"amount": 0.01, "success": True, "tx_hash": "0xab"}
    paid, amount, estimate = C._reconcile_paid_amount(
        paid=True, settle=settle,
        payment_info={"happened": True, "amount": 0.01, "authorized_amount": 0.01},
        probe_amount=0.01, max_amount_usd=1.0, url="http://fake/paid")
    assert (paid, amount, estimate) == (True, pytest.approx(0.01), False)


# --- the spend ledger books the signed amount --------------------------------

def test_cap_charge_prefers_the_signed_amount():
    res = X402Result(body="b", paid=True, amount_usd=1e24, tx_hash="0x", pay_to="0x",
                     status_code=200, authorized_amount_usd=0.01)
    assert x402_cap_charge(res, 1.0) == pytest.approx(0.01)


def test_cap_charge_without_signed_amount_clamps_to_the_ceiling():
    res = X402Result(body="b", paid=True, amount_usd=1e24, tx_hash="0x", pay_to="0x",
                     status_code=200)
    assert x402_cap_charge(res, 1.0) == pytest.approx(1.0)
    res = X402Result(body="b", paid=True, amount_usd=float("nan"), tx_hash="0x",
                     pay_to="0x", status_code=200)
    assert x402_cap_charge(res, 1.0) == pytest.approx(1.0)
    res = X402Result(body="b", paid=True, amount_usd=0.3, tx_hash="0x", pay_to="0x",
                     status_code=200)
    assert x402_cap_charge(res, 1.0) == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_hostile_header_books_one_cent_and_later_checks_pass():
    wallet = _wallet()
    res = X402Result(body="b", paid=True, amount_usd=1e24, tx_hash="0xtx",
                     pay_to="0xrecipient", status_code=200, amount_is_estimate=True,
                     authorized_amount_usd=0.01)
    out = await X402PayTool(wallet=wallet, client=_ResultClient(res)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=1))
    assert out.error is None
    assert wallet.policy.audit_log[-1]["amount_usd"] == pytest.approx(0.01)
    # Every rail still works afterwards: a later check of every venue passes.
    for venue in ("x402", "swap", "transfer"):
        assert wallet.policy.check(venue=venue, amount_usd=1.0,
                                   idempotency_key=f"later-{venue}").allowed


@pytest.mark.asyncio
async def test_natural_payment_books_its_signed_amount_unchanged():
    wallet = _wallet()
    res = X402Result(body="b", paid=True, amount_usd=0.05, tx_hash="0xtx",
                     pay_to="0xrecipient", status_code=200, authorized_amount_usd=0.05)
    out = await X402PayTool(wallet=wallet, client=_ResultClient(res)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=1))
    assert out.error is None
    assert wallet.policy.audit_log[-1]["amount_usd"] == pytest.approx(0.05)
    assert "[paid $0.0500 to 0xrecipient" in out.extracted_content


# --- x402out L2: the quote prices pinned USDC the way the SDK signs ----------

class _Ch:
    def __init__(self, decoded):
        hdr = base64.b64encode(json.dumps(decoded).encode()).decode()
        self.headers = {"PAYMENT-REQUIRED": hdr}
        self.status_code = 402
        self.text = ""
        self.content = b""


def _usdc():
    return C._CANONICAL_USDC_ASSET["mainnet"]


def test_pinned_usdc_ignores_seller_decimals():
    ch = {"x402Version": 2, "accepts": [{"amount": "1000000", "decimals": 12,
                                         "network": "eip155:8453", "asset": _usdc(),
                                         "payTo": "0xabc"}]}
    parsed = C._parse_402_challenge(_Ch(ch), configured="mainnet")
    assert parsed["amount"] == pytest.approx(1.0)


def test_pinned_usdc_without_version_reads_the_v2_field_like_the_sdk():
    ch = {"accepts": [{"maxAmountRequired": "1", "amount": "5000000",
                       "network": "eip155:8453", "asset": _usdc(), "payTo": "0xabc"}]}
    parsed = C._parse_402_challenge(_Ch(ch), configured="mainnet")
    assert parsed["amount"] == pytest.approx(5.0)


def test_pinned_usdc_v1_reads_max_amount_required():
    ch = {"x402Version": 1, "accepts": [{"maxAmountRequired": "250000", "amount": "1",
                                         "network": "base", "asset": _usdc(),
                                         "payTo": "0xabc"}]}
    parsed = C._parse_402_challenge(_Ch(ch), configured="mainnet")
    assert parsed["amount"] == pytest.approx(0.25)


# --- the signer's x402 venue daily cap is the agent's envelope ---------------

@pytest.mark.asyncio
async def test_signer_x402_daily_cap_refuses_before_any_payment(monkeypatch):
    import core.wallet.signer_envelope as env
    caps = {"x402_per_payment_usd": 5.0, "x402_daily_usd": 0.5}
    monkeypatch.setattr(env, "spend_held_by_signer", lambda: False)
    monkeypatch.setattr(env, "envelope", lambda leg: caps.get(leg))
    called = {}

    class Client(_ResultClient):
        async def fetch_with_payment(self, **kwargs):
            called["yes"] = True
            return await super().fetch_with_payment(**kwargs)

    res = X402Result(body="b", paid=True, amount_usd=0.05, tx_hash="0xtx",
                     pay_to="0xrecipient", status_code=200, authorized_amount_usd=0.05)
    wallet = _wallet()
    out = await X402PayTool(wallet=wallet, client=Client(res)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=1))
    assert out.error and "x402 daily cap" in out.error
    assert "yes" not in called
    # Within the signer's x402 daily cap the payment runs as before.
    caps["x402_daily_usd"] = 25.0
    out = await X402PayTool(wallet=wallet, client=Client(res)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=1))
    assert out.error is None and called.get("yes")


# --- 085 reval W4 (S-1): the signer approval lane is reachable under remote ---

def _caps_env(monkeypatch, caps):
    import core.wallet.signer_envelope as env
    monkeypatch.setattr(env, "spend_held_by_signer", lambda: False)
    monkeypatch.setattr(env, "envelope", lambda leg: caps.get(leg))


@pytest.mark.asyncio
async def test_shadow_refuses_above_the_signer_x402_cap_before_paying(monkeypatch):
    """Shadow signs locally: no signer approval can unlock a payment there."""
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    _caps_env(monkeypatch, {"x402_per_payment_usd": 5.0, "x402_daily_usd": 100.0})
    called = {}

    class Client(_ResultClient):
        async def fetch_with_payment(self, **kwargs):
            called["yes"] = True
            raise AssertionError("must not pay")

    out = await X402PayTool(wallet=_wallet(), client=Client(None)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=8))
    assert out.error and "x402 per-payment cap $5.00" in out.error
    assert "yes" not in called


@pytest.mark.asyncio
async def test_remote_forwards_above_the_cap_and_names_the_signer_approval(monkeypatch):
    """Remote: the signer decides. Above its cap it opens a root-only approval;
    the agent reports the id and the on-box verb, and nothing is booked."""
    from core.signer import protocol
    from core.signer.client import SignerRefused
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    _caps_env(monkeypatch, {"x402_per_payment_usd": 5.0, "x402_daily_usd": 6.0})
    called = {}

    class PaymentError(Exception):
        pass

    class Client(_ResultClient):
        async def fetch_with_payment(self, **kwargs):
            called["max"] = kwargs["max_amount_usd"]
            refusal = SignerRefused(
                protocol.APPROVAL_REQUIRED,
                "x402 payment $8.00 is above the signer's per-payment cap $5.00; it requires "
                "independent signer approval. The owner approves it ON THE BOX: `sudo polyrob "
                "owner promote signer_approval sig-0123456789`, then the same payment is "
                "retried. Nothing was signed.", {"approval_id": "sig-0123456789"})
            raise PaymentError(f"Failed to handle payment: {refusal}") from refusal

    wallet = _wallet()
    out = await X402PayTool(wallet=wallet, client=Client(None)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=8))
    assert called["max"] == 8
    assert out.error.startswith("x402 payment held by the wallet signer:")
    assert "promote signer_approval sig-0123456789" in out.error
    assert "Nothing was paid." in out.error
    assert [e for e in wallet.policy.audit_log if e["venue"] == "x402"] == []


@pytest.mark.asyncio
async def test_remote_within_the_cap_pays_unchanged(monkeypatch):
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    _caps_env(monkeypatch, {"x402_per_payment_usd": 5.0, "x402_daily_usd": 25.0})
    res = X402Result(body="b", paid=True, amount_usd=0.05, tx_hash="0xtx",
                     pay_to="0xrecipient", status_code=200, authorized_amount_usd=0.05)
    wallet = _wallet()
    out = await X402PayTool(wallet=wallet, client=_ResultClient(res)).x402_fetch(
        FetchParams(url="http://paid", max_amount_usd=1))
    assert out.error is None
    (row,) = [e for e in wallet.policy.audit_log if e["venue"] == "x402"]
    assert row["amount_usd"] == pytest.approx(0.05)
