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
