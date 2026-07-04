"""JWT authentication middleware."""

import logging
import jwt
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from api.auth_constants import is_admin

logger = logging.getLogger(__name__)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Decode JWT and populate request.state."""

    PUBLIC_PATHS = {
        "/", "/health", "/docs", "/openapi.json", "/redoc",
        "/api/auth/nonce", "/api/auth/verify",
        "/api/payments/pricing", "/api/x402/pricing"
    }

    def __init__(self, app, jwt_secret: str):
        super().__init__(app)
        self.jwt_secret = jwt_secret

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with JWT authentication."""

        # Skip public endpoints
        if (request.url.path in self.PUBLIC_PATHS or
            request.url.path.startswith("/static")):
            return await call_next(request)

        # Get JWT
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return await call_next(request)  # x402 flow

        token = auth_header[7:]

        try:
            # Decode JWT
            payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])

            # Populate request.state via the canonical C4 contract.
            from api.auth_state import set_auth_state
            set_auth_state(
                request.state,
                user_id=payload.get("user_id"),
                tier=payload.get("tier", "free"),
                role=payload.get("role", "user"),
                payment_method=None,
                authenticated=True,
            )
            # NOTE: "sub" contains wallet_address (JWT standard)
            request.state.wallet_address = payload.get("sub")
            request.state.chain = payload.get("chain", "ethereum")

            # Admin flag (simplified: only 'admin' role or admin wallet)
            request.state.is_admin = is_admin(
                role=request.state.role,
                wallet_address=request.state.wallet_address
            )

            wallet_display = payload.get('sub', '')[:10] if payload.get('sub') else 'unknown'
            logger.debug(f"JWT valid for {wallet_display}...")

        except jwt.ExpiredSignatureError:
            return JSONResponse(
                status_code=401,
                content={"error": "Token expired", "code": "TOKEN_EXPIRED"}
            )
        except jwt.InvalidTokenError:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid token", "code": "INVALID_TOKEN"}
            )

        return await call_next(request)
