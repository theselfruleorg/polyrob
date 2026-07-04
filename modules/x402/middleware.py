"""
x402 middleware for FastAPI - wraps fastapi-x402 with POLYROB user integration.

This middleware:
1. Uses fastapi-x402 for payment verification (via Coinbase facilitator)
2. Creates POLYROB user profiles for x402 payers
3. Sets request state for downstream handlers
"""

import logging
from typing import Callable, Optional
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import time

from .x402_integration import (
    generate_user_id_from_wallet,
    ensure_user_profile_for_payer,
    record_x402_payment,
    mark_payment_refund_due,
    should_refund_on_status,
    settlement_payment_id,
    get_x402_config,
    get_x402_price_usd,
)

logger = logging.getLogger(__name__)


def build_x402_challenge(request_path: str, cost_usd: Optional[float] = None) -> dict:
    """Build the standard x402 402-challenge body (the 'accepts' array).

    Single source of price (C2/F12): reads get_x402_price_usd() unless
    overridden. Used by the middleware's own early challenge (C7); a future
    pass may also route api/payment_verification.py::payment_required_response
    through this same builder for full byte-for-byte shape parity.
    """
    price_usd = cost_usd if cost_usd is not None else get_x402_price_usd()
    config = get_x402_config()
    network = config["network"]

    decimals = 6  # USDC/USDT default
    asset_address = None
    eip712_name = None
    eip712_version = None
    try:
        from fastapi_x402.networks import get_default_asset_config
        asset_config = get_default_asset_config(network)
        decimals = asset_config.decimals
        asset_address = asset_config.address
        eip712_name = asset_config.eip712_name
        eip712_version = asset_config.eip712_version
    except Exception:
        logger.debug(
            "fastapi_x402 asset config unavailable; using USDC-default decimals "
            "for the 402 challenge body"
        )

    amount_atomic = int(round(price_usd * (10 ** decimals)))

    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": network,
            "maxAmountRequired": str(amount_atomic),
            "resource": request_path,
            "description": f"API access: {request_path}",
            "mimeType": "application/json",
            "payTo": config["pay_to"],
            "maxTimeoutSeconds": 300,
            "asset": asset_address,
            "extra": {"name": eip712_name, "version": eip712_version},
        }],
        "amount_usd": price_usd,
    }


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Middleware to handle x402 payments with POLYROB user integration.

    Uses fastapi-x402 for payment verification, then integrates with
    POLYROB's user system.
    """

    def __init__(self, app, enabled: bool = True):
        """Initialize x402 middleware.

        Args:
            app: FastAPI app
            enabled: Whether x402 is enabled
        """
        super().__init__(app)
        self.enabled = enabled
        self.logger = logging.getLogger('x402.middleware')

        # Try to initialize fastapi-x402
        self._facilitator_client = None
        self._init_facilitator()

    # Anonymous x402 pay-per-request is supported ONLY on the A2A + OpenAI-compat
    # surfaces — NOT on the authenticated /api/task/* REST API. `fallback_auth_middleware`
    # (api/app.py, runs first/outermost) already gates every /api/* and /task/* path with a
    # 401 for anonymous callers by design (the REST API requires an account; we do not want
    # to punch an anonymity hole in it). So a `/task/sessions` entry here would be dead code —
    # mis-prefixed (the real mount is /api/task/sessions) AND pre-empted by fallback_auth even
    # if fixed. The 4 prefixes below are exactly the paths where the 402 challenge can
    # genuinely fire: each is a real mounted route that calls verify_payment_for_request()
    # and sits OUTSIDE fallback_auth_middleware's /api/ + /task/ gate. Anonymous callers who
    # want pay-per-request access should use A2A or /v1, not the REST API.
    X402_GATED_PATH_PREFIXES = ("/a2a/rpc", "/a2a/message/stream", "/a2a/tasks", "/v1/chat/completions")

    def _is_x402_gated(self, path: str) -> bool:
        return any(path.startswith(p) for p in self.X402_GATED_PATH_PREFIXES)

    def _init_facilitator(self):
        """Initialize the fastapi-x402 facilitator client."""
        try:
            from fastapi_x402 import init_x402, get_facilitator_client
            from fastapi_x402.networks import get_network_config

            config = get_x402_config()

            if not config["enabled"] or not config["pay_to"]:
                self.logger.info("x402 not enabled or pay_to not configured")
                return

            # Initialize fastapi-x402 global config
            # This sets up the facilitator based on network type
            import os
            os.environ.setdefault("PAY_TO_ADDRESS", config["pay_to"])
            os.environ.setdefault("X402_NETWORK", config["network"])

            if config["cdp_key_id"]:
                os.environ.setdefault("CDP_API_KEY_ID", config["cdp_key_id"])
            if config["cdp_key_secret"]:
                os.environ.setdefault("CDP_API_KEY_SECRET", config["cdp_key_secret"])

            # Don't auto-add middleware (we're doing custom integration)
            init_x402(
                app=None,
                pay_to=config["pay_to"],
                network=config["network"],
                auto_add_middleware=False,
                load_dotenv_file=False
            )

            # Get the facilitator client
            self._facilitator_client = get_facilitator_client()
            self.logger.info(f"x402 facilitator initialized for network: {config['network']}")

        except ImportError as e:
            self.logger.warning(f"fastapi-x402 not available: {e}")
        except Exception as e:
            self.logger.error(f"Failed to initialize x402 facilitator: {e}")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with x402 payment support."""

        # Skip if x402 disabled
        if not self.enabled:
            return await call_next(request)

        # Skip for public endpoints
        public_paths = [
            "/", "/health", "/docs", "/openapi.json",
            "/api/x402/pricing", "/.well-known/agent.json", "/a2a/agent-card"
        ]
        if request.url.path in public_paths or request.url.path.startswith("/api/x402/"):
            return await call_next(request)

        # Check for X-PAYMENT header (standard x402 format, base64 encoded)
        payment_header = request.headers.get("X-PAYMENT")

        if payment_header and self._facilitator_client:
            return await self._handle_x402_payment(request, call_next, payment_header)

        # C7: issue the standard 402 challenge for a genuinely-anonymous request
        # (no X-PAYMENT AND no other auth signal at all) hitting a gated path.
        # Credit/admin/JWT-authenticated callers carry an Authorization or
        # X-API-KEY header and are NOT touched here — they fall through to
        # call_next exactly as before, authorized downstream by
        # verify_payment_for_request.
        if not payment_header and self._is_x402_gated(request.url.path):
            config = get_x402_config()
            has_other_auth = bool(
                request.headers.get("Authorization")
                or request.headers.get("X-API-KEY")
                or request.cookies.get("auth_token")
            )
            if not has_other_auth and config.get("pay_to"):
                return JSONResponse(
                    status_code=402,
                    content=build_x402_challenge(request.url.path),
                )

        # No x402 payment -> continue to regular auth
        return await call_next(request)

    async def _handle_x402_payment(
        self,
        request: Request,
        call_next: Callable,
        payment_header: str
    ) -> Response:
        """Handle x402 payment verification and settlement.

        Args:
            request: FastAPI request
            call_next: Next middleware
            payment_header: Base64-encoded payment payload

        Returns:
            Response from endpoint or 402 error
        """
        try:
            from fastapi_x402.models import PaymentRequirements
            from fastapi_x402.networks import get_default_asset_config, get_network_config
            import json
            import base64

            config = get_x402_config()
            network = config["network"]

            # Decode payment header to get payer info
            try:
                payment_data = base64.b64decode(payment_header).decode("utf-8")
                payment_obj = json.loads(payment_data)
            except Exception as e:
                self.logger.warning(f"Failed to decode payment header: {e}")
                return JSONResponse(
                    status_code=402,
                    content={"error": "Invalid payment header format"}
                )

            # Extract payer address from payload
            payer_address = None
            if "payload" in payment_obj and "authorization" in payment_obj["payload"]:
                payer_address = payment_obj["payload"]["authorization"].get("from")

            if not payer_address:
                return JSONResponse(
                    status_code=402,
                    content={"error": "Missing payer address in payment payload"}
                )

            # Get asset config for network
            asset_config = get_default_asset_config(network)
            network_config = get_network_config(network)

            # Create payment requirements for verification.
            # Single source of truth for price (F12) — see get_x402_price_usd.
            amount_usd = get_x402_price_usd()
            amount_atomic = int(amount_usd * (10 ** asset_config.decimals))

            payment_requirements = PaymentRequirements(
                scheme="exact",
                network=network,
                maxAmountRequired=str(amount_atomic),
                resource=request.url.path,
                description=f"API access: {request.url.path}",
                mimeType="application/json",
                payTo=config["pay_to"],
                maxTimeoutSeconds=300,
                asset=asset_config.address,
                extra={
                    "name": asset_config.eip712_name,
                    "version": asset_config.eip712_version
                }
            )

            # Verify AND settle payment via facilitator
            verify_response, settle_response = await self._facilitator_client.verify_and_settle_payment(
                payment_header=payment_header,
                payment_requirements=payment_requirements
            )

            if not verify_response.isValid:
                error_msg = verify_response.error or "Payment verification failed"
                self.logger.warning(f"x402 verification failed: {error_msg}")
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "Payment verification failed",
                        "details": error_msg
                    }
                )

            if not settle_response.success:
                error_msg = settle_response.errorReason or "Payment settlement failed"
                self.logger.warning(f"x402 settlement failed: {error_msg}")
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "Payment settlement failed",
                        "details": error_msg
                    }
                )

            # Payment successful! Create user and proceed
            user_id = generate_user_id_from_wallet(payer_address)

            # Set request state via the canonical C4 contract.
            from api.auth_state import set_auth_state
            set_auth_state(
                request.state,
                user_id=user_id,
                tier="x402",
                role="user",
                payment_method="x402",
                authenticated=True,
            )
            request.state.payer_address = payer_address.lower()

            # Ensure user profile exists
            await ensure_user_profile_for_payer(payer_address, user_id)

            # Record the payment. Always record (N1 fix) — even a tx-less
            # settlement must leave a reconcilable row; never silently drop revenue.
            payment_id = settlement_payment_id(
                settle_response.transaction,
                payer_address,
                request.url.path,
                int(time.time() // 60),
            )
            await record_x402_payment(
                payment_id=payment_id,
                wallet_address=payer_address,
                user_id=user_id,
                amount_usd=amount_usd,
                network=network,
                recipient=config["pay_to"],
                transaction_hash=settle_response.transaction,
                amount_atomic=str(amount_atomic),
            )

            self.logger.info(
                f"x402 payment settled: {payer_address[:10]}... -> {user_id} "
                f"(tx: {settle_response.transaction[:16] if settle_response.transaction else 'none'}...)"
            )

            # Process request
            response = await call_next(request)

            # N4: payment already settled before this point. If the downstream
            # failed (5xx), the customer paid for nothing — flag for refund.
            if should_refund_on_status(response.status_code):
                await mark_payment_refund_due(payment_id)

            # Add x402 headers to response
            response.headers["X-PAYMENT-RESPONSE"] = "settled"
            if settle_response.transaction:
                response.headers["X-PAYMENT-TX"] = settle_response.transaction

            return response

        except Exception as e:
            self.logger.error(f"x402 payment processing error: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"error": f"Payment processing error: {str(e)}"}
            )
