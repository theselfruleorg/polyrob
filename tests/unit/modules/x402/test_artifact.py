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


# --- 046 Phase 0: the URI and the pay text read the invoice's own asset ------

def _inv(**kw):
    base = dict(request_id="inv_1", amount_usd=1.25, chain="base",
                recipient="0x" + "11" * 20, purpose="p",
                expires_at_epoch=9_999_999_999)
    base.update(kw)
    return base


def test_the_eip681_uri_names_the_invoices_own_token_and_chain_id(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    from modules.x402.artifact import build_payment_artifact
    uri = build_payment_artifact(_inv(
        chain="robinhood", asset_id="rob", asset_address="0x" + "bb" * 20,
        asset_decimals=18, amount_raw=7 * 10 ** 18, amount_usd=0.5,
    ))["pay_uri"]
    assert uri.startswith("ethereum:0x" + "bb" * 20)
    assert "@4663" in uri
    assert f"uint256={7 * 10 ** 18}" in uri
    assert ("address=0x" + "11" * 20) in uri


def test_a_usdc_base_invoice_produces_the_uri_it_always_did(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    from core.wallet.onchain import USDC_BASE_MAINNET
    from modules.x402.artifact import build_payment_artifact
    uri = build_payment_artifact(_inv(
        asset_id="usdc-base", asset_address=USDC_BASE_MAINNET,
        asset_decimals=6, amount_raw=1_250_000,
    ))["pay_uri"]
    assert uri == (f"ethereum:{USDC_BASE_MAINNET}@8453/transfer"
                   f"?address={'0x' + '11' * 20}&uint256=1250000")


def test_a_legacy_invoice_with_no_asset_columns_still_resolves_usdc(monkeypatch):
    """⚠️ A pre-046 invoice dict has no asset keys. It must keep producing the
    same URI, or an outstanding invoice's QR changes meaning mid-flight."""
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    from core.wallet.onchain import USDC_BASE_MAINNET
    from modules.x402.artifact import build_payment_artifact
    uri = build_payment_artifact(_inv())["pay_uri"]
    assert uri.startswith(f"ethereum:{USDC_BASE_MAINNET}@8453")
    assert "uint256=1250000" in uri


def test_the_address_style_is_unchanged_and_never_an_eip681_uri(monkeypatch):
    monkeypatch.setenv("INVOICE_QR_STYLE", "address")
    from modules.x402.artifact import build_payment_artifact
    assert build_payment_artifact(_inv())["pay_uri"] == "0x" + "11" * 20


def test_a_non_evm_invoice_falls_back_to_the_bare_address(monkeypatch):
    """⚠️ EIP-681 is an EVM URI. Emitting one for a Solana invoice would hand a
    payer a wallet link that resolves to nothing."""
    monkeypatch.setenv("INVOICE_QR_STYLE", "eip681")
    from modules.x402.artifact import build_payment_artifact
    uri = build_payment_artifact(_inv(
        chain="solana", recipient="SoLaNaAddr11111111111111111111111111111111",
        asset_id="usdc-solana", asset_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        asset_decimals=6, amount_raw=1_250_000,
    ))["pay_uri"]
    assert uri == "SoLaNaAddr11111111111111111111111111111111"


def test_the_pay_text_names_the_actual_token_not_always_usdc():
    """⚠️ A ROB invoice that reads 'Pay $0.50 USDC' tells the payer to send the
    wrong token."""
    from modules.x402.artifact import build_payment_artifact
    text = build_payment_artifact(_inv(
        chain="robinhood", asset_id="rob", asset_symbol="ROB",
        asset_address="0x" + "bb" * 20, asset_decimals=18,
        amount_raw=7 * 10 ** 18, amount_usd=0.5,
    ))["pay_text"]
    assert "ROB" in text
    assert "USDC" not in text
    assert "7" in text          # the token amount, not only the USD figure
