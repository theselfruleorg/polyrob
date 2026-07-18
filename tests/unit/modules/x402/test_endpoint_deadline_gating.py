"""x402 endpoint hardening — L5 (deadline), L6 (verify-status gate), H7 (pay
CancelledError reverts the settle claim).

Calls the route handlers directly with the invoicing seams + facilitator
mocked, mirroring tests/unit/api/test_x402_pay_endpoint.py. (Placed here under
the Task-3-owned tests/unit/modules/x402/ tree.)
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api import x402_endpoints as ep


def _fake_request(payment_header=None):
    return SimpleNamespace(headers={"X-PAYMENT": payment_header} if payment_header else {})


def _asset_cfg():
    return SimpleNamespace(decimals=6, address="0xasset", eip712_name="USDC", eip712_version="2")


def _pending_row(**over):
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc",
           "deadline": int(time.time()) + 3600}
    row.update(over)
    return row


# --- L5: deadline enforcement -------------------------------------------------

@pytest.mark.asyncio
async def test_l5_pay_past_deadline_410(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = _pending_row(deadline=int(time.time()) - 10)
    claim = AsyncMock(return_value=True)
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=claim):
        with pytest.raises(HTTPException) as ei:
            await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert ei.value.status_code == 410
    claim.assert_not_awaited()  # never even claimed a lapsed invoice


@pytest.mark.asyncio
async def test_l5_pay_within_deadline_still_works(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = _pending_row()  # future deadline
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=True)), \
         patch("modules.x402.invoicing.settle_payment_request", new=AsyncMock(return_value=True)), \
         patch.object(ep, "_verify_and_settle_invoice",
                      new=AsyncMock(return_value=(True, "0xtx", None))):
        resp = await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_l5_challenge_past_deadline_reports_expired(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = _pending_row(deadline=int(time.time()) - 10)
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        resp = await ep.get_invoice_challenge("inv_x")
    assert resp.status_code == 200  # NOT a 402 payable challenge
    assert json.loads(resp.body)["status"] == "expired"


@pytest.mark.asyncio
async def test_l5_challenge_within_deadline_still_402(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = _pending_row()
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        resp = await ep.get_invoice_challenge("inv_x")
    assert resp.status_code == 402


# --- L6: verify-status enablement gate ---------------------------------------

@pytest.mark.asyncio
async def test_l6_verify_status_gated_off_404(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "false")
    with pytest.raises(HTTPException) as ei:
        await ep.check_payment_status("nonce123", _fake_request())
    assert ei.value.status_code == 404


# --- H7: pay CancelledError reverts the settle claim -------------------------

@pytest.mark.asyncio
async def test_h7_pay_cancelled_error_reverts_claim(monkeypatch):
    """A client disconnect during the facilitator round-trip cancels the request
    task (CancelledError, a BaseException). The claim must still be reverted so
    the invoice stays payable, and the cancellation must propagate."""
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = _pending_row()
    revert = AsyncMock()

    async def cancel(*a, **k):
        raise asyncio.CancelledError()

    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=True)), \
         patch("modules.x402.invoicing.revert_settlement_claim", new=revert), \
         patch.object(ep, "_verify_and_settle_invoice", new=cancel):
        with pytest.raises(asyncio.CancelledError):
            await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    revert.assert_awaited_once()  # claim reverted despite the BaseException
