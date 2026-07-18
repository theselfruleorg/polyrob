"""Task 11 review fix C1 — payer-facing amount precision.

`_dedupe_amount_for_treasury` (`modules/x402/invoicing.py`) nudges the STORED
amount by deterministic sub-cent ($0.0001) steps to keep on-chain amount-
matching unambiguous. Prior to this fix, every payer-facing text surface
rendered the amount at 2dp (`${amount:.2f}`) — so a payer paying the
DISPLAYED "$7.50" for a jittered $7.5001 invoice would exact-match an OLDER,
unrelated $7.50 invoice on-chain (the watcher's oldest-first ambiguity
policy), settling the WRONG invoice across tenants. This asserts the fix:
every payer-facing surface renders FULL precision whenever sub-cent digits
are present, and never truncates a plain 2dp amount.
"""
import asyncio

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.artifact import build_payment_artifact, format_invoice_amount, invoice_qr_style
from tools.x402.invoice_tool import InvoiceParams, X402InvoiceTool

TREASURY = "0xTREASURYADDR000000000000000000000001"


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX", "INVOICE_QR_STYLE"):
        monkeypatch.delenv(var, raising=False)


# --- format_invoice_amount: the shared formatting helper --------------------

@pytest.mark.parametrize("amount, expected", [
    (7.5, "7.50"),
    (7.0, "7.00"),
    (0, "0.00"),
    (12.34, "12.34"),
    (7.5001, "7.5001"),
    (7.5002, "7.5002"),
    (10.00005, "10.00005"),
    (None, "0.00"),
])
def test_format_invoice_amount(amount, expected):
    assert format_invoice_amount(amount) == expected


# --- artifact.py: pay_text carries full precision ----------------------------

@pytest.mark.asyncio
async def test_pay_text_carries_full_precision_when_jittered(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("X402_INVOICE_AMOUNT_JITTER", "true")
    monkeypatch.setenv("INVOICE_QR_STYLE", "address")  # pin the text-only style under test
    db = await _setup_db(tmp_path)
    try:
        first = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=7.5, purpose="a", db=db)
        second = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=7.5, purpose="b", db=db)
        assert second["amount_usd"] != 7.5  # jitter fired — precondition for this test

        first_art = build_payment_artifact(first)
        second_art = build_payment_artifact(second)

        # The OLDER invoice's text still shows the plain 2dp amount...
        assert "$7.50" in first_art["pay_text"]
        # ...but the jittered invoice's text must NOT collapse to "$7.50" too —
        # that would be exactly the cross-tenant misdirection this fix closes.
        assert "$7.50 " not in second_art["pay_text"] + " "  # no bare "$7.50" token
        assert f"${format_invoice_amount(second['amount_usd'])}" in second_art["pay_text"]
        assert format_invoice_amount(second["amount_usd"]) != "7.50"
    finally:
        await db.close()


# --- invoice_tool.py: x402_request result text carries full precision -------

class _Ctx:
    user_id = "rob"
    session_id = "sess_1"


def test_x402_request_content_carries_full_precision_when_jittered(monkeypatch):
    async def fake_create(**kw):
        return {"request_id": "inv_jit", "amount_usd": 7.5001, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending"}

    monkeypatch.setattr(invoicing, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=7.5001, purpose="widget"), execution_context=_Ctx()))
    assert res.error is None
    assert "$7.5001" in res.extracted_content
    assert "$7.50 " not in res.extracted_content + " "


def test_x402_request_content_still_renders_plain_2dp_when_not_jittered(monkeypatch):
    """Byte-identical for the common (non-colliding) case — full precision
    only kicks in when sub-cent digits are actually present."""
    async def fake_create(**kw):
        return {"request_id": "inv_plain", "amount_usd": 5.0, "asset": "usdc",
                "chain": "base", "recipient": "0xT", "purpose": kw["purpose"],
                "expires_at_epoch": 123, "status": "pending"}

    monkeypatch.setattr(invoicing, "create_payment_request", fake_create)
    res = asyncio.run(X402InvoiceTool().x402_request(
        InvoiceParams(amount_usd=5.0, purpose="widget"), execution_context=_Ctx()))
    assert res.error is None
    assert "$5.00" in res.extracted_content


# --- eip681 preference when on-chain detection is active --------------------

def test_eip681_preferred_when_detection_on_and_style_unset(monkeypatch):
    monkeypatch.delenv("INVOICE_QR_STYLE", raising=False)
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    assert invoice_qr_style() == "eip681"


def test_address_style_stays_default_when_detection_off(monkeypatch):
    monkeypatch.delenv("INVOICE_QR_STYLE", raising=False)
    monkeypatch.delenv("X402_SETTLE_ONCHAIN_DETECT", raising=False)
    assert invoice_qr_style() == "address"


def test_explicit_address_style_wins_even_when_detection_on(monkeypatch):
    """An operator's EXPLICIT INVOICE_QR_STYLE is never silently overridden —
    only the un-set default is upgraded."""
    monkeypatch.setenv("INVOICE_QR_STYLE", "address")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    assert invoice_qr_style() == "address"


def test_eip681_qr_encodes_exact_atomic_amount_when_detection_on(monkeypatch):
    monkeypatch.delenv("INVOICE_QR_STYLE", raising=False)
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    invoice = {
        "request_id": "inv_abc123", "amount_usd": 7.5001, "asset": "usdc",
        "chain": "base", "recipient": "0x1234567890abcdef1234567890abcdef12345678",
        "purpose": "widget", "expires_at_epoch": 1770000000, "status": "pending",
    }
    art = build_payment_artifact(invoice)
    assert art["pay_uri"].startswith("ethereum:")
    assert "uint256=7500100" in art["pay_uri"]  # exact atomic units, no rounding loss
