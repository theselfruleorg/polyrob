"""Always-on validator for self-service ``rob_xxx`` API keys (B2).

The keys minted by ``POST /api/auth/api-keys`` had exactly ONE validator:
``AuthenticationMiddleware._validate_api_key``. That middleware is only mounted
when ``API_SECRET``/``ADMIN_TOKEN`` is set (``api/app.py``), so on the default
deployment a freshly minted key authenticated nothing — the agent card
advertised ``X-API-KEY: rob_xxx…`` as the *recommended* scheme for machine
callers and every such call 401'd.

This middleware is registered UNCONDITIONALLY. It validates against the
``api_keys`` table (the same hashed lookup), sets the canonical auth state, and
otherwise gets out of the way: no key, an unknown key, or an already
authenticated request all fall straight through to the existing gates. It never
REFUSES anything — refusal stays the job of the downstream auth middlewares, so
mounting this cannot make a previously-working request fail.
"""

import hashlib
import logging
import time
from typing import Any, Callable, Dict, Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from utils.bounded_collections import BoundedDict

logger = logging.getLogger(__name__)

#: Prefix every self-service key carries (``modules/auth/api_key_manager``).
API_KEY_PREFIX = "rob_"

#: Seconds a validated key stays cached. Short: a revoked key must stop working
#: without a restart, and the DB lookup is one indexed row.
_CACHE_TTL_SEC = 60.0


def looks_like_api_key(value: Optional[str]) -> bool:
    """Whether ``value`` is shaped like a self-service POLYROB API key.

    Deliberately narrow: this middleware must never try to interpret the
    operator SERVICE token or a JWT that happens to arrive in the same header.
    """
    return bool(value) and value.startswith(API_KEY_PREFIX) and len(value) >= 32


def extract_api_key(request: Request) -> Optional[str]:
    """The ``rob_xxx`` key on this request, from ``X-API-KEY`` or a Bearer token."""
    header = request.headers.get("X-API-KEY")
    if looks_like_api_key(header):
        return header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if looks_like_api_key(token):
            return token
    return None


async def validate_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Look ``api_key`` up in the ``api_keys`` table.

    Returns the auth-state dict on success, ``None`` when the key is unknown,
    inactive, expired, or the database is unavailable. Never raises: an
    unreadable store means "this middleware cannot authenticate you", which
    leaves the request exactly as it found it.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    try:
        from core.container import DependencyContainer

        container = DependencyContainer.get_instance()
        db = container.get_service("database_manager") if container else None
        if not db:
            return None
        row = await db.fetch_one(
            """
            SELECT user_id, scopes, is_active, expires_at
            FROM api_keys
            WHERE key_hash = ? AND is_active = 1
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """,
            (key_hash,),
        )
        if not row:
            return None
        try:
            await db.execute(
                "UPDATE api_keys SET last_used = CURRENT_TIMESTAMP WHERE key_hash = ?",
                (key_hash,),
            )
        except Exception as e:  # last_used is telemetry, never a gate
            logger.debug("api key last_used update failed: %s", e)
        return {
            "user_id": row["user_id"],
            "tier": "free",
            "role": "user",
            "auth_method": "api_key",
        }
    except Exception as e:
        logger.warning("api key validation unavailable: %s", e)
        return None


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate a ``rob_xxx`` key on every request, ENABLE_AUTH or not."""

    def __init__(self, app):
        super().__init__(app)
        self._cache: BoundedDict[str, Dict[str, Any]] = BoundedDict(max_size=1000)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if getattr(request.state, "authenticated", False):
            return await call_next(request)

        api_key = extract_api_key(request)
        if not api_key:
            return await call_next(request)

        cache_key = hashlib.sha256(api_key.encode()).hexdigest()
        now = time.monotonic()
        info = None
        cached = self._cache.get(cache_key)
        if cached and cached.get("_expires_at", 0) > now:
            info = cached
        else:
            info = await validate_api_key(api_key)
            if info is not None:
                info = dict(info, _expires_at=now + _CACHE_TTL_SEC)
                self._cache[cache_key] = info

        if info is not None:
            from api.auth_state import set_auth_state

            set_auth_state(
                request.state,
                user_id=info["user_id"],
                tier=info.get("tier", "free"),
                role=info.get("role", "user"),
                payment_method=None,
                authenticated=True,
            )
            request.state.is_admin = False
            request.state.auth_method = "api_key"

        return await call_next(request)
