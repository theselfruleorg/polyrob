"""The EIP-712 domain an inbound x402 challenge names is the domain the token checks.

fastapi-x402 0.1.8 names Base MAINNET USDC "USDC" (the Base Sepolia token's name). The
mainnet FiatToken's domain is "USD Coin" / "2" (the x402 SDK pins it). A payer signs the
domain the challenge names, so the old challenge produced an authorization the token
rejects. The machine-payment challenge, its verification requirement and the invoice
challenge all take the domain from ``modules.x402.middleware.eip712_domain``.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA
from modules.x402.middleware import build_x402_challenge, eip712_domain


def test_base_mainnet_usdc_is_usd_coin_v2():
    assert eip712_domain("base", USDC_BASE_MAINNET, "USDC", "2") == ("USD Coin", "2")


def test_base_sepolia_usdc_keeps_usdc():
    assert eip712_domain("base-sepolia", USDC_BASE_SEPOLIA, "x", "y") == ("USDC", "2")


def test_an_unknown_token_keeps_the_given_domain():
    assert eip712_domain("base", "0x" + "ab" * 20, "Given", "9") == ("Given", "9")
    assert eip712_domain("base", None, "Given", "9") == ("Given", "9")


def test_the_machine_challenge_names_the_token_domain(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "22" * 20)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    extra = build_x402_challenge("/a2a/rpc", cost_usd=0.05)["accepts"][0]["extra"]
    assert extra == {"name": "USD Coin", "version": "2"}


@pytest.mark.asyncio
async def test_the_invoice_challenge_names_the_token_domain(monkeypatch):
    from api import x402_endpoints as ep
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 1.0, "chain": "base", "recipient": "0xt",
           "purpose": "svc", "request_id": "inv_x"}
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)):
        resp = await ep.get_invoice_challenge("inv_x")
    assert resp.status_code == 402
    accepts = json.loads(resp.body)["accepts"][0]
    assert accepts["asset"].lower() == USDC_BASE_MAINNET.lower()
    assert accepts["extra"] == {"name": "USD Coin", "version": "2"}


def test_the_invoice_requirement_uses_the_same_rule():
    from api import x402_endpoints as ep
    cfg = SimpleNamespace(address=USDC_BASE_MAINNET, eip712_name="USDC", eip712_version="2")
    assert ep._invoice_domain("base", cfg) == {"name": "USD Coin", "version": "2"}
