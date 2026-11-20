"""Payable invoice endpoint — public verify+settle for an agent-created invoice.

Calls the route handlers directly with the invoicing seams + facilitator mocked
(no network, no DB container). Contract: disabled → 404; unknown → 404;
non-pending → 409; missing X-PAYMENT → 402; facilitator success → settle once + 200;
settle-race → 409. The payment_settled event/wake are the watcher's job (not asserted here).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api import x402_endpoints as ep


def _fake_request(payment_header=None):
    return SimpleNamespace(headers={"X-PAYMENT": payment_header} if payment_header else {})


def _asset_cfg():
    return SimpleNamespace(decimals=6, address="0xasset", eip712_name="USDC", eip712_version="2")


@pytest.mark.asyncio
async def test_pay_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "false")
    with pytest.raises(HTTPException) as ei:
        await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_pay_unknown_invoice_404(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ei:
            await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_pay_non_pending_409(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value={"status": "completed"})):
        with pytest.raises(HTTPException) as ei:
            await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_pay_missing_header_402_carries_the_challenge(monkeypatch):
    """An x402 payer POSTs /pay unpaid first, then re-sends the SAME request with the
    X-PAYMENT it signed against THIS answer: the 402 must carry the invoice's `accepts`
    (a bare detail string left every x402 client unable to pay an invoice)."""
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc", "request_id": "inv_x"}
    claim = AsyncMock(return_value=True)
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=claim), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        resp = await ep.pay_invoice("inv_x", _fake_request(payment_header=None))
    assert resp.status_code == 402
    import json as _json
    accepts = _json.loads(resp.body)["accepts"][0]
    assert accepts["maxAmountRequired"] == str(5_000_000)
    assert accepts["payTo"] == "0xt"
    assert accepts["resource"] == "/api/x402/requests/inv_x/pay"
    claim.assert_not_awaited()  # an unpaid POST claims nothing


@pytest.mark.asyncio
async def test_pay_facilitator_success_settles_once(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc"}
    settle = AsyncMock(return_value=True)
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=True)), \
         patch("modules.x402.invoicing.settle_payment_request", new=settle), \
         patch.object(ep, "_verify_and_settle_invoice",
                      new=AsyncMock(return_value=(True, "0xtx", None))):
        resp = await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert resp.status_code == 200
    settle.assert_awaited_once()
    assert settle.await_args.kwargs["transaction_hash"] == "0xtx"


@pytest.mark.asyncio
async def test_pay_claim_lost_returns_409_without_facilitator(monkeypatch):
    # A concurrent payer already claimed (pending->settling): 409, and this loser
    # NEVER touches the facilitator (so it can't settle on-chain and lose funds).
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc"}
    verify = AsyncMock(return_value=(True, "0xtx", None))
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=False)), \
         patch.object(ep, "_verify_and_settle_invoice", new=verify):
        with pytest.raises(HTTPException) as ei:
            await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert ei.value.status_code == 409
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_challenge_pending_returns_402_with_accepts(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc"}
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=row)), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        resp = await ep.get_invoice_challenge("inv_x")
    assert resp.status_code == 402
    import json as _json
    body = _json.loads(resp.body)
    assert body["accepts"][0]["maxAmountRequired"] == str(5_000_000)  # 5 USDC, 6 decimals
    assert body["accepts"][0]["payTo"] == "0xt"


@pytest.mark.asyncio
async def test_challenge_settled_returns_200_status(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value={"status": "completed"})):
        resp = await ep.get_invoice_challenge("inv_x")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pay_invalid_payment_402(monkeypatch):
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc"}
    revert = AsyncMock()
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=True)), \
         patch("modules.x402.invoicing.revert_settlement_claim", new=revert), \
         patch.object(ep, "_verify_and_settle_invoice",
                      new=AsyncMock(return_value=(False, None, "bad sig"))):
        with pytest.raises(HTTPException) as ei:
            await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert ei.value.status_code == 402
    revert.assert_awaited_once()  # claim reverted -> invoice stays payable


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,moved", [("offer_order", True), ("agent_invoice", False)])
async def test_pay_moves_an_order_now_without_the_watcher(monkeypatch, kind, moved):
    """F11: a completed ORDER invoice pays its order in the /pay request itself, so
    the order moves with API_AUTONOMY_RUNTIME off (no settlement watcher here)."""
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": "0xt", "purpose": "svc", "kind": kind}
    move = AsyncMock()
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=True)), \
         patch("modules.x402.invoicing.settle_payment_request", new=AsyncMock(return_value=True)), \
         patch.object(ep, "_verify_and_settle_invoice",
                      new=AsyncMock(return_value=(True, "0xtx", None))), \
         patch.object(ep, "_move_paid_order", new=move):
        resp = await ep.pay_invoice("inv_x", _fake_request(payment_header="hdr"))
    assert resp.status_code == 200
    assert move.await_count == (1 if moved else 0)


@pytest.mark.asyncio
async def test_move_paid_order_never_fails_the_paid_request():
    """The money is recorded; a failure to move the order now is logged and left
    to the watcher, never turned into an error for a payer who paid."""
    with patch("modules.x402.settlement_watcher.SettlementWatcher.settle_order_now",
               new=AsyncMock(side_effect=RuntimeError("store down"))):
        await ep._move_paid_order("inv_x")
