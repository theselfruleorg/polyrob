"""x402 payment endpoints.

Provides endpoints for x402 pay-per-request cryptocurrency payments.
External AI agents can use this to pay for POLYROB services without creating accounts.

Flow (the standard x402 header handshake, handled by X402PaymentMiddleware):
1. Agent sends a request to any gated endpoint.
2. On HTTP 402, POLYROB returns payment requirements (address, amount, nonce) in
   the response body.
3. Agent signs a payment authorization with their wallet.
4. Agent retries the SAME request with an X-PAYMENT header. POLYROB verifies and
   settles the payment automatically, then executes the request.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import logging
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/x402", tags=["x402-payments"])


class PaymentRequestInfo(BaseModel):
    """Payment request information."""
    id: str
    amount_usd: float
    asset: str
    chain: str
    status: str
    created_at: str
    expires_at: str


class PaymentHistoryResponse(BaseModel):
    """Payment history for a wallet."""
    payer_address: str
    total_spent_usd: float
    total_requests: int
    payments: List[PaymentRequestInfo]


@router.get("/pricing")
async def get_x402_pricing():
    """Get x402 pricing information (public endpoint).

    This is the first endpoint an external agent should call to understand
    payment options and pricing.
    """
    # Try both env var names for payment address
    recipient = os.environ.get("X402_PAYMENT_RECIPIENT", os.environ.get("X402_PAYMENT_ADDRESS", ""))
    facilitator = os.environ.get("X402_FACILITATOR_URL", "")

    from modules.x402.x402_integration import get_x402_price_usd
    return {
        "payment_method": "x402",
        "description": "Pay-per-request with cryptocurrency. No account required.",
        "pricing": {
            # Single source of truth (F12) — matches the live middleware charge.
            "per_request_usd": get_x402_price_usd(),
            "minimum_purchase_usd": 0,
            "supported_assets": ["usdc", "usdt", "eth"],
            "supported_chains": ["base", "ethereum"]
        },
        "payment_address": recipient if recipient else "Not configured",
        "facilitator": facilitator if facilitator else "Direct payment",
        "flow": {
            "1_send_request": "Send your request to any gated endpoint",
            "2_receive_402": "On HTTP 402, read the payment requirements from the response body",
            "3_retry_with_xpayment": (
                "Retry the SAME request with an X-PAYMENT header (base64-encoded "
                "EIP-3009 authorization). Settlement is handled automatically."
            ),
        },
        "benefits": [
            "No account registration required",
            "Pay only for what you use",
            "Instant access",
            "Privacy-preserving"
        ]
    }


@router.get("/verify-status/{nonce}")
async def check_payment_status(nonce: str, request: Request):
    """Check the status of a payment by nonce.

    Agents can poll this endpoint to verify their payment was received.
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    result = await db.fetch_one("""
        SELECT id, status, amount_usd, asset, chain,
               payer_address, transaction_hash, created_at, completed_at
        FROM x402_payment_requests
        WHERE nonce = ?
    """, (nonce,))

    if not result:
        raise HTTPException(status_code=404, detail="Payment request not found")

    return {
        "payment_request_id": result['id'],
        "status": result['status'],
        "amount_usd": result['amount_usd'],
        "asset": result['asset'],
        "chain": result['chain'],
        "payer_address": result['payer_address'],
        "transaction_hash": result['transaction_hash'],
        "created_at": result['created_at'],
        "completed_at": result['completed_at']
    }


@router.get("/payment-history/{wallet_address}")
async def get_payment_history(
    wallet_address: str,
    request: Request,
    limit: int = 50
) -> PaymentHistoryResponse:
    """Get payment history for a wallet address."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Get payment history
    payments = await db.fetch_all("""
        SELECT id, amount_usd, asset, chain, status, created_at, deadline
        FROM x402_payment_requests
        WHERE payer_address = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (wallet_address.lower(), limit))

    # Calculate totals
    total = await db.fetch_one("""
        SELECT
            COUNT(*) as count,
            COALESCE(SUM(amount_usd), 0) as total_spent
        FROM x402_payment_requests
        WHERE payer_address = ? AND status = 'completed'
    """, (wallet_address.lower(),))

    from datetime import datetime

    return PaymentHistoryResponse(
        payer_address=wallet_address,
        total_spent_usd=float(total['total_spent']) if total else 0,
        total_requests=total['count'] if total else 0,
        payments=[
            PaymentRequestInfo(
                id=p['id'],
                amount_usd=p['amount_usd'],
                asset=p['asset'],
                chain=p['chain'],
                status=p['status'],
                created_at=p['created_at'],
                expires_at=datetime.fromtimestamp(p['deadline']).isoformat()
            )
            for p in payments
        ]
    )


def _invoice_asset_cfg(network: str):
    """Resolve the default USDC asset config for a network (decimals + EIP-712 name)."""
    from fastapi_x402.networks import get_default_asset_config
    return get_default_asset_config(network)


@router.get("/requests/{request_id}")
async def get_invoice_challenge(request_id: str):
    """Public: return the x402 payment challenge for an agent-created invoice.

    A pending invoice yields HTTP 402 with a per-invoice ``accepts`` block (the
    payer signs against it and POSTs to ``/pay``); a settled/expired invoice
    returns 200 with its status. Gated by X402_INVOICE_ENABLED (404 when off)."""
    from modules.x402.invoicing import get_payment_request, x402_invoicing_enabled
    if not x402_invoicing_enabled():
        raise HTTPException(status_code=404, detail="invoicing disabled")
    row = await get_payment_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown invoice")
    if row["status"] != "pending":
        return JSONResponse({"request_id": request_id, "status": row["status"]})
    cfg = _invoice_asset_cfg(row["chain"])
    atomic = int(round(float(row["amount_usd"]) * (10 ** cfg.decimals)))
    accepts = [{
        "scheme": "exact", "network": row["chain"], "maxAmountRequired": str(atomic),
        "resource": f"/api/x402/requests/{request_id}/pay", "description": row["purpose"],
        "mimeType": "application/json", "payTo": row["recipient"], "maxTimeoutSeconds": 300,
        "asset": cfg.address, "extra": {"name": cfg.eip712_name, "version": cfg.eip712_version},
    }]
    return JSONResponse(
        {"x402Version": 1, "accepts": accepts, "amount_usd": row["amount_usd"]},
        status_code=402)


@router.post("/requests/{request_id}/pay")
async def pay_invoice(request_id: str, request: Request):
    """Public: a third party settles an agent-created invoice via the facilitator.

    Verifies + settles the X-PAYMENT header against a per-invoice
    PaymentRequirements, then flips the row pending→completed. The payment_settled
    event and originating-session wake are emitted by the settlement watcher (the
    single wake producer), NOT here. Gated by X402_INVOICE_ENABLED."""
    from modules.x402.invoicing import (
        get_payment_request, settle_payment_request, claim_for_settlement,
        revert_settlement_claim, x402_invoicing_enabled)
    if not x402_invoicing_enabled():
        raise HTTPException(status_code=404, detail="invoicing disabled")
    row = await get_payment_request(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown invoice")
    if row["status"] != "pending":
        raise HTTPException(status_code=409,
                            detail=f"invoice not payable (status={row['status']})")
    payment_header = request.headers.get("X-PAYMENT")
    if not payment_header:
        raise HTTPException(status_code=402, detail="X-PAYMENT header required")

    # Claim the exclusive right to settle BEFORE touching the facilitator, so two
    # concurrent distinct payers can never both settle this invoice on-chain (the
    # loser fails the CAS and never calls the facilitator).
    if not await claim_for_settlement(request_id):
        raise HTTPException(status_code=409, detail="invoice already being settled")
    try:
        ok_verify, tx, error = await _verify_and_settle_invoice(request_id, row, payment_header)
    except Exception:
        await revert_settlement_claim(request_id)  # facilitator error -> payable again
        raise
    if not ok_verify:
        await revert_settlement_claim(request_id)  # verify/settle rejected -> payable again
        raise HTTPException(status_code=402, detail=error or "payment rejected")
    ok = await settle_payment_request(request_id, transaction_hash=tx)  # settling -> completed
    if not ok:
        raise HTTPException(status_code=409, detail="already settled")
    return JSONResponse({"request_id": request_id, "status": "completed", "transaction": tx})


async def _verify_and_settle_invoice(request_id, row, payment_header):
    """Build a per-invoice PaymentRequirements and run it through the facilitator.

    Isolated so the endpoint tests can stub the facilitator (fastapi_x402 is a
    prod-only dependency). Returns (verified_and_settled, tx_hash, error_detail).
    A facilitator/network error raises HTTPException(502) — never settles."""
    from fastapi_x402 import init_x402, get_facilitator_client
    from fastapi_x402.models import PaymentRequirements
    cfg = _invoice_asset_cfg(row["chain"])
    atomic = int(round(float(row["amount_usd"]) * (10 ** cfg.decimals)))
    payment_requirements = PaymentRequirements(
        scheme="exact", network=row["chain"], maxAmountRequired=str(atomic),
        resource=f"/api/x402/requests/{request_id}/pay", description=row["purpose"],
        mimeType="application/json", payTo=row["recipient"], maxTimeoutSeconds=300,
        asset=cfg.address, extra={"name": cfg.eip712_name, "version": cfg.eip712_version})
    try:
        init_x402(app=None, pay_to=row["recipient"], network=row["chain"],
                  auto_add_middleware=False, load_dotenv_file=False)
        client = get_facilitator_client()
        verify_resp, settle_resp = await client.verify_and_settle_payment(
            payment_header=payment_header, payment_requirements=payment_requirements)
    except Exception as e:  # facilitator/network error — do not settle
        logger.warning("x402 invoice %s facilitator error: %s", request_id, e)
        raise HTTPException(status_code=502, detail="payment facilitator error")
    if not getattr(verify_resp, "isValid", False):
        return (False, None, getattr(verify_resp, "error", None) or "invalid payment")
    if not getattr(settle_resp, "success", False):
        return (False, None, getattr(settle_resp, "errorReason", None) or "settlement failed")
    return (True, getattr(settle_resp, "transaction", None), None)
