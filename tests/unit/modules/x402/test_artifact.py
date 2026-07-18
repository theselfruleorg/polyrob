"""build_payment_artifact — pay_text facts + pay_uri QR-style resolution (Task 6).

INVOICE_QR_STYLE selects what the invoice card's QR code encodes: the bare
treasury address (default, works with any wallet's "scan to pay" address
import) or an EIP-681 USDC transfer URI (pre-fills amount + contract, scan
straight into a send). pay_text must carry the same facts already shown in
tools/x402/invoice_tool.py's x402_request result text.
"""
from core.wallet.onchain import USDC_BASE_MAINNET, USDC_BASE_SEPOLIA
from modules.x402.artifact import build_payment_artifact, invoice_qr_style

_INVOICE = {
    "request_id": "inv_abc123",
    "amount_usd": 12.34,
    "asset": "usdc",
    "chain": "base",
    "recipient": "0x1234567890abcdef1234567890abcdef12345678",
    "purpose": "research report",
    "expires_at_epoch": 1770000000,
    "status": "pending",
}


def _clean_env(monkeypatch):
    monkeypatch.delenv("INVOICE_QR_STYLE", raising=False)


def test_default_style_is_address(monkeypatch):
    _clean_env(monkeypatch)
    assert invoice_qr_style() == "address"


def test_address_style_pay_uri_is_bare_recipient(monkeypatch):
    _clean_env(monkeypatch)
    art = build_payment_artifact(_INVOICE)
    assert art["pay_uri"] == _INVOICE["recipient"]


def test_pay_text_carries_all_facts(monkeypatch):
    _clean_env(monkeypatch)
    art = build_payment_artifact(_INVOICE)
    text = art["pay_text"]
    assert _INVOICE["request_id"] in text
    assert "12.34" in text
    assert "usdc" in text.lower()
    assert _INVOICE["chain"] in text
    assert _INVOICE["recipient"] in text
    assert _INVOICE["purpose"] in text
    assert "2026-02-02" in text  # expiry, human YYYY-MM-DD from epoch


def test_eip681_style_atomic_math(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    art = build_payment_artifact(_INVOICE)
    uri = art["pay_uri"]
    assert uri is not None
    assert uri.startswith("ethereum:")
    assert USDC_BASE_MAINNET in uri
    assert "@8453/transfer" in uri
    assert f"address={_INVOICE['recipient']}" in uri
    # $12.34 -> 12340000 atomic units (6 decimals)
    assert "uint256=12340000" in uri


def test_eip681_uses_sepolia_contract_for_testnet_chain(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    inv = dict(_INVOICE, chain="base-sepolia")
    art = build_payment_artifact(inv)
    assert USDC_BASE_SEPOLIA in art["pay_uri"]
    assert "@84532/transfer" in art["pay_uri"]


def test_invalid_style_value_falls_back_to_address(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "bogus-style")
    assert invoice_qr_style() == "address"
    art = build_payment_artifact(_INVOICE)
    assert art["pay_uri"] == _INVOICE["recipient"]


def test_style_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "EIP681")
    assert invoice_qr_style() == "eip681"


def test_pay_uri_none_when_no_recipient(monkeypatch):
    _clean_env(monkeypatch)
    inv = dict(_INVOICE, recipient="")
    art = build_payment_artifact(inv)
    assert art["pay_uri"] is None


def test_pay_uri_none_when_no_recipient_eip681(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    inv = dict(_INVOICE, recipient="")
    art = build_payment_artifact(inv)
    assert art["pay_uri"] is None


def test_zero_amount_atomic_is_zero(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    inv = dict(_INVOICE, amount_usd=0)
    art = build_payment_artifact(inv)
    assert "uint256=0" in art["pay_uri"]
