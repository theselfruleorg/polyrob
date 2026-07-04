"""Unified payment verification supporting credits and x402."""

from fastapi import Request, HTTPException
from typing import Tuple, Dict, Any
import logging
from api.auth_constants import is_admin, extract_admin_info

logger = logging.getLogger(__name__)


async def verify_payment_for_request(
    request: Request,
    cost_credits: int = 1
) -> Tuple[str, Dict[str, Any]]:
    """Verify payment via credits OR x402 OR admin bypass.

    Returns:
        Tuple of (payment_method: str, details: dict)

    Raises:
        HTTPException: If no valid payment found
    """

    # OPTION 0: Admin bypass (if enabled)
    user_id = getattr(request.state, 'user_id', None)
    
    # Use centralized admin detection
    is_admin_user, role, wallet_address = extract_admin_info(request.state)

    logger.info(f"🔍 Payment verification: user={user_id}, role={role}, wallet={wallet_address}, is_admin={is_admin_user}")

    # Check if admin bypass is enabled
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    config = container.config if container else None

    # Admin bypass is enabled by default (config defaults to True)
    bypass_enabled = True
    if config:
        bypass_enabled = getattr(config, 'bypass_payment_for_admins', True)
    
    if bypass_enabled and is_admin_user:
        logger.info(f"✅ Admin {user_id} (role: {role}, wallet: {wallet_address}) bypassed payment check")
        return "admin_bypass", {
            "user_id": user_id,
            "role": role,
            "wallet": wallet_address,
            "bypass_reason": "admin_privilege",
            "endpoint": request.url.path
        }

    # OPTION 1: Check for x402 payment FIRST (middleware already verified)
    # This must come before credit check because x402 users have user_id set
    payment_method = getattr(request.state, 'payment_method', None)

    if payment_method == "x402":
        # Payment verified by X402PaymentMiddleware
        payer_address = getattr(request.state, 'payer_address', 'unknown')
        logger.info(f"✅ x402 payment already verified for {payer_address[:10]}...")
        return "x402", {
            "payer_address": payer_address,
            "user_id": user_id,
            "endpoint": request.url.path
        }

    # OPTION 2: Check for JWT (credit system)
    if user_id and user_id not in ['api_user', 'authenticated_api_user']:
        # User authenticated → use credit system.
        # C1: AUTHORIZE ONLY. Do NOT deduct here — the per-token LLMUsageTracker
        # (modules/credits/usage_tracker.py::record_llm_usage) is the single
        # deduction path, billed on ACTUAL token usage once the call completes.
        # Deducting a flat cost_credits here too was double-billing every request.
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        balance_mgr = container.get_service('balance_manager')

        if not balance_mgr:
            raise HTTPException(status_code=503, detail="Credit system unavailable")

        has_credits = await balance_mgr.has_sufficient_balance(user_id, cost_credits)

        if not has_credits:
            raise HTTPException(
                status_code=402,
                detail="Insufficient credits. Deposit more or use x402."
            )

        return "credits", {
            "user_id": user_id,
            "credits_deducted": 0,
            "endpoint": request.url.path
        }

    # OPTION 3: No payment → Return 402
    raise HTTPException(
        status_code=402,
        detail="Payment required. Use credits (login) or x402 (pay-per-request)."
    )


def payment_required_response(
    request: Request,
    cost_credits: int = 1
) -> Dict[str, Any]:
    """Generate 402 response with both payment options."""
    from modules.x402.x402_integration import get_x402_price_usd

    cost_usd = cost_credits * 0.01
    # C2: single price SSOT — the quoted x402 price MUST equal the live charge.
    x402_cost_usd = get_x402_price_usd()

    # Get x402 handler from app state
    payment_handler = getattr(request.app.state, 'x402_handler', None)

    x402_details = {}
    if payment_handler:
        x402_response = payment_handler.create_payment_required_response(
            amount_usd=x402_cost_usd,
            asset="usdc",
            metadata={"endpoint": request.url.path, "cost_credits": cost_credits}
        )
        x402_details = x402_response['body']['payment']

    return {
        "error": "Payment Required",
        "code": "PAYMENT_REQUIRED",
        "payment_options": {
            "credits": {
                "cost_usd": cost_usd,
                "cost_credits": cost_credits,
                "instructions": "Login with wallet at /api/auth/verify"
            },
            "x402": {
                "cost_usd": x402_cost_usd,
                "payment_details": x402_details
            }
        }
    }
