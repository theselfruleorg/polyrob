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

    # OPTION 0b: the operator SERVICE token (B4). It used to arrive as
    # role="admin" and take the branch above — i.e. `API_AUTH_TOKEN` was a full
    # admin credential. It now carries its own role, so it keeps the one thing
    # it legitimately needs (a machine-to-machine caller is not billed
    # per-request) and none of the admin privilege.
    from api.auth_constants import is_service_role
    if bypass_enabled and is_service_role(role):
        logger.info("✅ service token bypassed payment check for %s", request.url.path)
        return "service_bypass", {
            "user_id": user_id,
            "role": role,
            "bypass_reason": "operator_service_token",
            "endpoint": request.url.path,
        }

    # OPTION 1: Check for x402 payment FIRST (middleware already verified)
    # This must come before credit check because x402 users have user_id set
    payment_method = getattr(request.state, 'payment_method', None)

    if payment_method == "x402":
        from core.billing_context import mark_billed
        mark_billed()
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
        # Every compute surface reaches this gate, including A2A, /v1 and chat.
        # A welcome credit grant alone must not bypass the instance's access tier.
        import os
        if os.environ.get("ENVIRONMENT", "production") != "development":
            from api.auth_constants import has_full_access
            tier_manager = container.get_service("tier_manager") if container else None
            if tier_manager is None:
                raise HTTPException(status_code=503, detail="Account tier verification unavailable")
            try:
                tier = await tier_manager.get_user_tier(user_id)
            except Exception as exc:
                raise HTTPException(status_code=503, detail="Account tier verification unavailable") from exc
            if not has_full_access(tier):
                raise HTTPException(status_code=403, detail="Compute access requires an eligible account tier")
        # User authenticated → use credit system.
        # Admission does not charge a flat request fee. Each model call reserves
        # before inference; the usage tracker settles its actual cost once.
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

        from core.billing_context import mark_billed
        from modules.credits.reservations import credit_reserver
        mark_billed(credit_reserver(balance_mgr, user_id))
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
    """Generate 402 response with every payment option: credits, plus each
    contributed option (067 P5a, ``api/contributions.py``; today x402)."""
    from api.contributions import payment_options
    cost_usd = cost_credits * 0.01
    options: Dict[str, Any] = {
        "credits": {
            "cost_usd": cost_usd,
            "cost_credits": cost_credits,
            "instructions": "Login with wallet at /api/auth/verify"
        },
    }
    for name, build in payment_options():
        options[name] = build(request, cost_credits)
    return {
        "error": "Payment Required",
        "code": "PAYMENT_REQUIRED",
        "payment_options": options,
    }
