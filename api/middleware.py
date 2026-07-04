"""Middleware components for API authentication and rate limiting."""

import time
import hashlib
import hmac
import logging
from typing import Dict, Any, Optional, Callable
from collections import defaultdict, deque
from datetime import datetime, timedelta

from fastapi import Request, HTTPException, Header
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from api.models import ErrorResponse, RateLimitInfo
from core.permissions import Permissions
from utils.bounded_collections import BoundedDict


logger = logging.getLogger(__name__)


class RateLimiter:
    """Rate limiter implementation using token bucket algorithm."""

    def __init__(
        self,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        burst_size: int = 10
    ):
        """Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute
            requests_per_hour: Maximum requests per hour
            burst_size: Maximum burst requests allowed
        """
        self.rpm_limit = requests_per_minute
        self.rph_limit = requests_per_hour
        self.burst_size = burst_size

        # Token buckets per user
        self.user_buckets: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                'tokens': float(burst_size),
                'last_refill': time.monotonic(),
                'minute_count': 0,
                'hour_count': 0,
                'minute_reset': time.monotonic(),
                'hour_reset': time.monotonic()
            }
        )

    def check_rate_limit(self, user_id: str) -> tuple[bool, Optional[RateLimitInfo]]:
        """Check if user is within rate limits using token bucket.

        Returns:
            Tuple of (allowed, rate_limit_info)
        """
        now = time.monotonic()
        bucket = self.user_buckets[user_id]

        # Refill tokens based on time elapsed
        time_since_refill = now - bucket['last_refill']
        refill_rate = self.burst_size / 1.0  # tokens per second
        tokens_to_add = time_since_refill * refill_rate
        bucket['tokens'] = min(self.burst_size, bucket['tokens'] + tokens_to_add)
        bucket['last_refill'] = now

        # Reset minute/hour counters if windows expired
        if now - bucket['minute_reset'] >= 60:
            bucket['minute_count'] = 0
            bucket['minute_reset'] = now

        if now - bucket['hour_reset'] >= 3600:
            bucket['hour_count'] = 0
            bucket['hour_reset'] = now

        # Check if we have tokens available
        if bucket['tokens'] < 1.0:
            return False, self._get_rate_limit_info(user_id, "burst", bucket)

        # Check minute limit
        if bucket['minute_count'] >= self.rpm_limit:
            return False, self._get_rate_limit_info(user_id, "minute", bucket)

        # Check hour limit
        if bucket['hour_count'] >= self.rph_limit:
            return False, self._get_rate_limit_info(user_id, "hour", bucket)

        # Consume a token and increment counters
        bucket['tokens'] -= 1.0
        bucket['minute_count'] += 1
        bucket['hour_count'] += 1

        return True, self._get_rate_limit_info(user_id, "ok", bucket)

    def _cleanup_old_buckets(self) -> None:
        """Clean up old user buckets to prevent memory leak."""
        now = time.monotonic()
        # Remove buckets not used in the last hour
        cutoff = now - 3600
        to_remove = [
            user_id for user_id, bucket in self.user_buckets.items()
            if bucket['last_refill'] < cutoff
        ]
        for user_id in to_remove:
            del self.user_buckets[user_id]

    def _get_rate_limit_info(self, user_id: str, status: str, bucket: Dict[str, Any]) -> RateLimitInfo:
        """Get rate limit information for user."""
        now = time.monotonic()

        if status == "burst":
            # Calculate when tokens will be available
            tokens_needed = 1.0 - bucket['tokens']
            refill_rate = self.burst_size / 1.0  # tokens per second
            seconds_until_token = tokens_needed / refill_rate
            return RateLimitInfo(
                limit=self.burst_size,
                remaining=0,
                reset_at=datetime.now() + timedelta(seconds=seconds_until_token),
                window="burst"
            )
        elif status == "minute":
            seconds_until_reset = 60 - (now - bucket['minute_reset'])
            return RateLimitInfo(
                limit=self.rpm_limit,
                remaining=0,
                reset_at=datetime.now() + timedelta(seconds=seconds_until_reset),
                window="minute"
            )
        elif status == "hour":
            seconds_until_reset = 3600 - (now - bucket['hour_reset'])
            return RateLimitInfo(
                limit=self.rph_limit,
                remaining=0,
                reset_at=datetime.now() + timedelta(seconds=seconds_until_reset),
                window="hour"
            )
        else:
            # Return current status
            minute_remaining = max(0, self.rpm_limit - bucket['minute_count'])
            seconds_until_reset = 60 - (now - bucket['minute_reset'])
            return RateLimitInfo(
                limit=self.rpm_limit,
                remaining=minute_remaining,
                reset_at=datetime.now() + timedelta(seconds=seconds_until_reset),
                window="minute"
            )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware for rate limiting API requests."""

    def __init__(self, app, **kwargs):
        """Initialize rate limit middleware."""
        super().__init__(app)
        self.rate_limiter = RateLimiter(**kwargs)
        self.logger = logger

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with rate limiting."""
        # Skip rate limiting for health checks and docs
        if request.url.path in ["/health", "/docs", "/openapi.json"]:
            return await call_next(request)

        # Get user identifier (from auth header, IP, or session)
        user_id = self._get_user_identifier(request)

        # Check rate limit
        allowed, rate_info = self.rate_limiter.check_rate_limit(user_id)

        if not allowed:
            self.logger.warning(f"Rate limit exceeded for user {user_id}")
            # Properly serialize rate_info to avoid datetime JSON errors
            rate_info_dict = {}
            if rate_info:
                rate_info_dict = rate_info.dict()
                # Ensure datetime fields are properly serialized
                if 'reset_at' in rate_info_dict and rate_info_dict['reset_at']:
                    rate_info_dict['reset_at'] = rate_info_dict['reset_at'].isoformat()
                if 'reset_time' in rate_info_dict and rate_info_dict['reset_time']:
                    rate_info_dict['reset_time'] = rate_info_dict['reset_time'].isoformat()
            
            headers = {}
            if rate_info:
                headers.update({
                    "X-RateLimit-Limit": str(rate_info.limit),
                    "X-RateLimit-Remaining": str(rate_info.remaining or 0),
                })
                if rate_info.reset_at:
                    headers["X-RateLimit-Reset"] = rate_info.reset_at.isoformat()
                    retry_after = int((rate_info.reset_at - datetime.now()).total_seconds())
                    headers["Retry-After"] = str(max(1, retry_after))  # Ensure at least 1 second
                    
            return JSONResponse(
                status_code=429,
                content=ErrorResponse(
                    error="Rate limit exceeded",
                    code="RATE_LIMIT_EXCEEDED",
                    details=rate_info_dict
                ).dict(),
                headers=headers
            )

        # Process request
        response = await call_next(request)

        # Add rate limit headers to response
        if rate_info:
            response.headers["X-RateLimit-Limit"] = str(rate_info.limit)
            response.headers["X-RateLimit-Remaining"] = str(rate_info.remaining or 0)
            if rate_info.reset_at:
                response.headers["X-RateLimit-Reset"] = rate_info.reset_at.isoformat()

        return response

    def _get_user_identifier(self, request: Request) -> str:
        """Get user identifier from request."""
        # Try to get from Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header:
            return hashlib.md5(auth_header.encode()).hexdigest()[:16]

        # Try to get from API key header
        api_key = request.headers.get("X-API-Key", "")
        if api_key:
            return hashlib.md5(api_key.encode()).hexdigest()[:16]

        # Fall back to IP address
        client_host = request.client.host if request.client else "unknown"
        return f"ip_{client_host}"


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Middleware for API authentication."""

    def __init__(
        self,
        app,
        secret_key: Optional[str] = None,
        permissions_manager: Optional[Permissions] = None
    ):
        """Initialize authentication middleware.

        Args:
            app: The ASGI application
            secret_key: Secret key for token validation (REQUIRED)
            permissions_manager: Optional permissions manager

        Raises:
            ValueError: If secret_key is not provided
        """
        super().__init__(app)

        # SECURITY: Require secret key - no defaults
        if not secret_key or secret_key.strip() == "":
            raise ValueError(
                "SECURITY: secret_key is required for AuthenticationMiddleware. "
                "Set JWT_SECRET_KEY environment variable."
            )
        self.secret_key = secret_key

        self.permissions = permissions_manager
        self.logger = logger

        # Cache for validated tokens
        self.token_cache: BoundedDict[str, Dict[str, Any]] = BoundedDict(max_size=1000)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request with authentication."""
        # Skip auth for public endpoints
        public_paths = ["/", "/health", "/docs", "/openapi.json", "/api/auth"]
        if request.url.path in public_paths:
            return await call_next(request)

        # Get authentication credentials
        auth_header = request.headers.get("Authorization", "")
        api_key = request.headers.get("X-API-Key", "")

        # Validate authentication
        user_info = await self._validate_auth(auth_header, api_key)

        if not user_info:
            self.logger.warning(f"Unauthorized access attempt to {request.url.path}")
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error="Unauthorized",
                    code="UNAUTHORIZED",
                    details={"message": "Invalid or missing authentication credentials"}
                ).dict(),
                headers={"WWW-Authenticate": "Bearer"}
            )

        # Add user info to request state
        from api.auth_state import set_auth_state
        set_auth_state(
            request.state,
            user_id=user_info.get("user_id"),
            tier=user_info.get("tier", "free"),
            role=user_info.get("role", "user"),
            payment_method=None,
            authenticated=True,
        )
        # Back-compat: request.state.user (dict) had exactly one reader
        # (api/app.py's fallback skip-check, now migrated to `authenticated` in
        # this same task) — kept here in case an external consumer still reads it.
        request.state.user = user_info

        # Check permissions for specific endpoints
        if not await self._check_permissions(request, user_info):
            return JSONResponse(
                status_code=403,
                content=ErrorResponse(
                    error="Forbidden",
                    code="FORBIDDEN",
                    details={"message": "Insufficient permissions"}
                ).dict()
            )

        # Process request
        return await call_next(request)

    async def _validate_auth(
        self,
        auth_header: str,
        api_key: str
    ) -> Optional[Dict[str, Any]]:
        """Validate authentication credentials."""
        # Check Bearer token (JWT)
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

            # Check cache first
            if token in self.token_cache:
                return self.token_cache[token]

            # Check if it looks like a JWT (three base64 parts)
            if token.count('.') == 2:
                try:
                    import jwt
                    import os
                    jwt_secret = os.environ.get("JWT_SECRET_KEY", self.secret_key)

                    decoded = jwt.decode(token, jwt_secret, algorithms=["HS256"])
                    user_info = {
                        "user_id": decoded.get("sub", decoded.get("user_id", "unknown")),
                        "authenticated": True,
                        "permissions": ["read", "write"],
                        "role": decoded.get("role", "user"),
                        "tier": decoded.get("tier", "free"),
                        "admin_wallet": decoded.get("admin_wallet", False)
                    }

                    self.token_cache[token] = user_info
                    return user_info

                except Exception as e:
                    self.logger.warning(f"JWT validation failed: {e}")
                    return None
            else:
                # Non-JWT bearer token - validate with HMAC
                try:
                    expected = hmac.new(
                        self.secret_key.encode(),
                        token.encode(),
                        hashlib.sha256
                    ).hexdigest()

                    user_info = {
                        "user_id": token[:8],
                        "authenticated": True,
                        "permissions": ["read", "write"]
                    }

                    self.token_cache[token] = user_info
                    return user_info

                except Exception as e:
                    self.logger.error(f"Token validation error: {e}")
                    return None

        # Check API key - validate against database
        if api_key:
            return await self._validate_api_key(api_key)

        return None

    async def _validate_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        """Validate API key against database.

        SECURITY: Proper API key validation using hashed comparison.
        """
        import hashlib

        # Basic format validation
        if not api_key or len(api_key) < 32:
            return None

        # Check cache first
        cache_key = f"apikey_{hashlib.sha256(api_key.encode()).hexdigest()[:16]}"
        if cache_key in self.token_cache:
            return self.token_cache[cache_key]

        try:
            # Hash the API key for database lookup
            key_hash = hashlib.sha256(api_key.encode()).hexdigest()

            # Try to get database and validate
            from core.container import DependencyContainer
            container = DependencyContainer.get_instance()
            db = container.get_service('database_manager')

            if db:
                # Look up API key by hash
                result = await db.fetch_one("""
                    SELECT user_id, scopes, is_active, expires_at
                    FROM api_keys
                    WHERE key_hash = ? AND is_active = 1
                    AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """, (key_hash,))

                if result:
                    # Update last_used timestamp
                    await db.execute("""
                        UPDATE api_keys SET last_used = CURRENT_TIMESTAMP
                        WHERE key_hash = ?
                    """, (key_hash,))

                    user_info = {
                        "user_id": result['user_id'],
                        "authenticated": True,
                        "permissions": ["read", "write"],
                        "auth_method": "api_key"
                    }

                    # Cache the validated key
                    self.token_cache[cache_key] = user_info
                    return user_info

            # If database not available or key not found, reject
            self.logger.warning(f"API key validation failed: key not found in database")
            return None

        except Exception as e:
            self.logger.error(f"API key database validation error: {e}")
            return None

    async def _check_permissions(
        self,
        request: Request,
        user_info: Dict[str, Any]
    ) -> bool:
        """Check if user has required permissions for endpoint.

        NOTE: Admin check for /api/admin endpoints is handled by:
        1. JWTAuthMiddleware sets request.state.is_admin
        2. admin_endpoints.py uses require_admin() dependency

        This method provides basic read/write permission checks.
        """
        # Admin endpoints - check is_admin flag set by JWT middleware
        if request.url.path.startswith("/api/admin"):
            # Use role/admin_wallet from user_info (populated from JWT)
            from api.auth_constants import is_admin
            return is_admin(
                role=user_info.get("role"),
                wallet_address=user_info.get("wallet_address")
            )

        # Regular endpoints - check basic permissions
        required_permission = "write" if request.method in ["POST", "PUT", "DELETE"] else "read"
        user_permissions = user_info.get("permissions", [])
        return required_permission in user_permissions


def create_api_key(user_id: str, secret_key: str) -> str:
    """Create an API key for a user."""
    # Create timestamp
    timestamp = str(int(time.time()))

    # Create signature
    message = f"{user_id}:{timestamp}"
    signature = hmac.new(
        secret_key.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    # Combine into API key
    return f"{user_id}:{timestamp}:{signature}"


def validate_api_key(api_key: str, secret_key: str, max_age_seconds: int = 86400) -> Optional[str]:
    """Validate an API key and return user_id if valid."""
    try:
        parts = api_key.split(":")
        if len(parts) != 3:
            return None

        user_id, timestamp, signature = parts

        # Check age
        key_age = int(time.time()) - int(timestamp)
        if key_age > max_age_seconds:
            return None

        # Verify signature
        message = f"{user_id}:{timestamp}"
        expected_signature = hmac.new(
            secret_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        if hmac.compare_digest(signature, expected_signature):
            return user_id

    except Exception as e:
        logger.error(f"API key validation error: {e}")

    return None