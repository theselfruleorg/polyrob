"""046 Phase 0 must be INVISIBLE on a deployment that pinned no asset.

Asserted, not claimed. Every row in this file is a way the widening could have
changed behaviour for a plain USDC-on-Base deployment.
"""
import pytest


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "11" * 20)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("PAYMENT_DEFAULT_ASSET", "X402_SETTLE_ONCHAIN_DETECT",
                "X402_INVOICE_DAILY_MAX", "AUTONOMY_HALT"):
        monkeypatch.delenv(var, raising=False)


def test_no_operator_row_means_every_path_resolves_usdc_base():
    from modules.x402.invoicing import resolve_invoice_asset
    row = resolve_invoice_asset("base", None)
    assert row.asset_id == "usdc-base"
    assert row.decimals == 6
    assert row.rail == "facilitator"
    assert row.source == "builtin"


@pytest.mark.asyncio
async def test_a_default_invoice_row_is_shaped_exactly_as_before(x402_db):
    from modules.x402 import invoicing
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=2.5, purpose="p", db=x402_db)
    row = await x402_db.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
    assert row["asset"] == "usdc"
    assert row["chain"] == "base"
    assert row["amount_usd"] == 2.5
    assert row["status"] == "pending"


def test_the_scan_key_for_the_default_asset_is_still_the_bare_treasury():
    """⚠️ A key change re-seeds the checkpoint near head and silently skips
    every transfer in the gap."""
    from core.payments.assets import resolve
    from modules.x402.settlement_scan import scan_key
    usdc = resolve("usdc-base")
    assert scan_key("0xabc", "base", usdc.address) == "0xabc"


def test_the_default_challenge_is_still_the_facilitator_shape():
    from api.x402_endpoints import _challenge_for_invoice
    body, status = _challenge_for_invoice({
        "request_id": "inv_1", "amount_usd": 1.25, "chain": "base",
        "recipient": "0x" + "11" * 20, "purpose": "p", "asset_id": None,
        "asset_address": None, "asset_decimals": None, "amount_raw": None})
    assert status == 402
    assert body["accepts"][0]["scheme"] == "exact"


def test_the_default_pay_text_still_says_usdc():
    from modules.x402.artifact import build_payment_artifact
    text = build_payment_artifact({
        "request_id": "inv_1", "amount_usd": 1.25, "chain": "base",
        "recipient": "0x" + "11" * 20, "purpose": "p",
        "expires_at_epoch": 9_999_999_999})["pay_text"]
    assert "$1.25 USDC" in text


def test_the_legacy_asset_column_still_reads_usdc_for_a_default_invoice():
    """Every existing reader displays this column; a USDC invoice must keep
    reading exactly 'usdc'."""
    from core.payments.assets import resolve
    assert (resolve("usdc-base").symbol or "").lower() == "usdc"


def test_every_new_flag_has_a_catalog_row():
    from core.flags_catalog import CATALOG
    names = {row[0] for row in CATALOG}
    for flag in ("PAYMENT_ASSETS_ENABLED", "PAYMENT_DEFAULT_ASSET",
                 "PAYMENT_QUOTE_MAX_AGE_SEC"):
        assert flag in names, f"{flag} has no docs/CONFIGURATION.md row"
