"""Free wallet authentication endpoints using SIWE (Sign-In with Ethereum).

Replaces expensive Privy with free industry-standard SIWE.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
import jwt
import uuid
from datetime import datetime, timedelta
import logging
import os
import re
from api.auth_constants import is_admin_role, is_admin_wallet
from core.token_denylist import OWNER_COOKIE_TTL_SECONDS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["authentication"])

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_eth_address(value: str) -> str:
    """Validate + EIP-55 checksum-normalize an Ethereum address.

    Normalizing canonicalizes casing so the same wallet maps to ONE identity
    (the address becomes the JWT sub / user_id / admin-allowlist key downstream).
    Raises ValueError on a malformed address (-> 422 at the request boundary).
    """
    if not isinstance(value, str) or not _ADDR_RE.match(value.strip()):
        raise ValueError("invalid Ethereum address")
    try:
        from eth_utils import to_checksum_address
        return to_checksum_address(value.strip())
    except Exception:
        # eth_utils missing/odd input: keep validation, skip checksum normalization.
        return value.strip()


class NonceRequest(BaseModel):
    """Request nonce for wallet authentication."""
    wallet_address: str
    chain_id: int = 1  # Ethereum mainnet by default

    @field_validator("wallet_address")
    @classmethod
    def _check_addr(cls, v: str) -> str:
        return normalize_eth_address(v)


class NonceResponse(BaseModel):
    """SIWE message and nonce."""
    message: str
    nonce: str
    issued_at: str
    expiration: str


class VerifyRequest(BaseModel):
    """Verify wallet signature."""
    wallet_address: str
    message: str
    signature: str
    nonce: str
    chain: str = 'ethereum'

    @field_validator("wallet_address")
    @classmethod
    def _check_addr(cls, v: str) -> str:
        return normalize_eth_address(v)


class AuthResponse(BaseModel):
    """Authentication response with JWT."""
    token: str
    user_id: str
    wallet_address: str
    role: str
    tier: str
    expires_at: str


@router.post("/nonce", response_model=NonceResponse)
async def get_nonce(request: NonceRequest):
    """
    Generate SIWE message and nonce for wallet authentication.

    FREE - no third-party service needed!

    Example:
        POST /api/auth/nonce
        {
            "wallet_address": "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb",
            "chain_id": 137
        }
    """

    import os

    from api.dependencies import optional_container

    # B19 (revalidation): resolve the container through the tolerant seam —
    # `DependencyContainer.get_instance()` RAISES on a process whose lifespan
    # never ran, so the honest 503 below was unreachable and the caller got a
    # 500 traceback on exactly the instance the message is written for.
    container = optional_container()
    siwe_auth = container.get_service('siwe_authenticator') if container else None

    if not siwe_auth:
        # B19: a wallet login on an instance with the account system OFF is a
        # CONFIGURATION state, not a server fault — 500 told the caller to
        # retry something that can never succeed.
        raise HTTPException(
            status_code=503,
            detail=("Wallet sign-in is not enabled on this instance. "
                    "Set ENABLE_AUTH=true (and configure a database) to turn "
                    "on the account system, then restart."),
        )

    # Get domain from environment or default to the local webview
    domain = os.environ.get("WEBVIEW_DOMAIN", "localhost:3000")
    uri = f"https://{domain}"

    # Create SIWE message
    result = await siwe_auth.create_siwe_message(
        wallet_address=request.wallet_address,
        domain=domain,
        uri=uri,
        chain_id=request.chain_id
    )

    return NonceResponse(**result)


@router.post("/verify")
async def verify_signature(request: VerifyRequest):
    """Verify wallet signature and return JWT with HTTP-only cookie."""

    from api.dependencies import optional_container

    # B19 (revalidation): see /nonce — the container accessor raises on an
    # uninitialized process, so it is resolved through the tolerant seam and
    # the 503 below is reachable.
    container = optional_container()
    siwe_auth = container.get_service('siwe_authenticator') if container else None
    identity_mapper = container.get_service('identity_mapper') if container else None
    db = container.get_service('database_manager') if container else None

    if not siwe_auth or not identity_mapper or not db:
        # B19: the twin of /nonce — a disabled account system is 503 with the
        # remedy, never a 500 that invites a retry.
        raise HTTPException(
            status_code=503,
            detail=("Wallet sign-in is not enabled on this instance. "
                    "Set ENABLE_AUTH=true (and configure a database) to turn "
                    "on the account system, then restart."),
        )

    # Verify signature
    is_valid = await siwe_auth.verify_signature(
        wallet_address=request.wallet_address,
        message=request.message,
        signature=request.signature,
        nonce=request.nonce
    )

    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Get or create user - ALL wallets can sign in (tier set based on token ownership)
    # Feature-level access control enforced elsewhere (task sessions, etc.)
    user_id = await identity_mapper.get_or_create_user(
        wallet_address=request.wallet_address,
        chain=request.chain
    )

    # Get user details
    user = await db.fetch_one("""
        SELECT role, tier FROM user_profiles WHERE user_id = ?
    """, (user_id,))

    role = user['role'] if user else 'user'
    tier = user['tier'] if user else 'free'

    # SECURITY FIX: Do NOT auto-escalate admin role
    # Admin wallets get admin PRIVILEGES during the session but NOT persistent role change
    # This prevents silent role escalation without audit trail
    # To make someone a permanent admin, use the /admin/users/{id}/role endpoint
    is_admin_by_wallet = is_admin_wallet(request.wallet_address)
    if is_admin_by_wallet:
        logger.info(f"Admin wallet detected: {request.wallet_address[:6]}...{request.wallet_address[-4:]} (granting session privileges, not changing role)")
        try:
            from modules.database.audit_log import AuditLogger
            await AuditLogger(db).log_admin_wallet_auth(
                wallet_address=request.wallet_address, user_id=user_id,
            )
        except Exception as audit_exc:
            logger.warning(f"Failed to write admin-wallet audit trail: {audit_exc}")

    # Create JWT
    jwt_secret = container.config.jwt_secret_key
    if not jwt_secret:
        raise HTTPException(status_code=500, detail="JWT not configured")

    # SECURITY FIX: Never log JWT secret, even partially
    # Logging even 20 chars significantly reduces brute-force difficulty

    # W5 (043): short (≤24h) lifetime + a random jti so /logout can revoke this
    # token server-side. Shares OWNER_COOKIE_TTL_SECONDS with the owner-login
    # minter (webview/owner_auth.py) so the two cookie lifetimes never drift.
    expires_at = datetime.utcnow() + timedelta(seconds=OWNER_COOKIE_TTL_SECONDS)

    # Include admin_wallet flag in token for session-based admin privileges
    # This allows admin wallets to have privileges without modifying the role in DB
    token_payload = {
        "sub": request.wallet_address,      # WALLET AS PRIMARY! ✅
        "user_id": user_id,                 # Internal DB ID
        "chain": request.chain,
        "tier": tier,
        "role": role,
        "admin_wallet": is_admin_by_wallet,  # Session-based admin flag
        "jti": uuid.uuid4().hex,
        "iat": datetime.utcnow(),
        "exp": expires_at
    }

    token = jwt.encode(token_payload, jwt_secret, algorithm="HS256")

    # Log authentication success without sensitive details
    admin_note = " [ADMIN WALLET]" if is_admin_by_wallet else ""
    logger.info(f"✅ Authenticated wallet {request.wallet_address[:6]}...{request.wallet_address[-4:]} (role: {role}, tier: {tier}){admin_note}")

    # Create response with auth data
    response_data = {
        "token": token,
        "user_id": user_id,
        "wallet_address": request.wallet_address,
        "role": role,
        "tier": tier,
        # The ONE admin predicate (core.constants.ADMIN_ROLES), not a literal.
        # A hardcoded admin-role literal is the same H1 shape the audit
        # removed from three other gates: it misses "owner", so an owner-login
        # response said is_admin=false and every client hid the admin surface.
        "is_admin": is_admin_by_wallet or is_admin_role(role),
        "expires_at": expires_at.isoformat()
    }

    # Create JSON response
    response = JSONResponse(content=response_data)

    # SERVER-SIDE COOKIE SETTING (more reliable than client-side)
    # SECURITY: Use secure cookie settings based on environment
    # - secure=True: Cookie only sent over HTTPS (nginx terminates SSL)
    # - httponly=True: Prevents JavaScript access (XSS protection)
    # - samesite="lax": CSRF protection while allowing navigation
    is_production = os.environ.get("ENVIRONMENT", "production") == "production"
    secure = is_production  # Only require HTTPS in production
    httponly = True
    samesite = "lax"
    max_age = OWNER_COOKIE_TTL_SECONDS  # W5: ≤24h, agrees with the owner minter

    logger.debug(f"🍪 Setting auth cookie: secure={secure}, httponly={httponly}, samesite={samesite}, is_production={is_production}")

    response.set_cookie(
        key="auth_token",
        value=token,
        max_age=max_age,
        path="/",
        # domain parameter intentionally omitted - defaults to current domain
        secure=secure,
        httponly=httponly,
        samesite=samesite
    )

    return response


@router.get("/me")
async def get_current_user(request: Request):
    """
    Get current authenticated user info.

    Requires Authorization: Bearer <token> header.
    """

    # B45 (revalidation): `hasattr` is True the moment ANY middleware touched
    # the attribute — including when it set it to None or "" — so an
    # unauthenticated caller read as authenticated. The VALUE decides.
    user_id = getattr(request.state, 'user_id', None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from api.dependencies import optional_container

    container = optional_container()
    tier_manager = container.get_service('tier_manager') if container else None

    if tier_manager:
        user_info = await tier_manager.get_user_info(user_id)
        return user_info
    # No account system on this instance: say who the caller is and with what
    # role, rather than nothing. `role` is what decides admin/service access.
    return {
        "user_id": user_id,
        "role": getattr(request.state, 'role', 'user'),
        "tier": getattr(request.state, 'tier', 'free'),
    }


# =============================================================================
# API Key Management (Option A: Self-Service for AI Agents)
# =============================================================================

class CreateAPIKeyRequest(BaseModel):
    """Request to create a new API key."""
    name: str = "Default"
    expires_days: int = None  # None = never expires


class APIKeyResponse(BaseModel):
    """Response with API key (shown only once!)."""
    api_key: str
    name: str
    prefix: str
    expires_at: str = None
    created_at: str
    warning: str


class APIKeyInfo(BaseModel):
    """API key info (without the actual key)."""
    prefix: str
    name: str
    created_at: str
    last_used: str = None
    expires_at: str = None
    is_active: bool


@router.post("/api-keys", response_model=APIKeyResponse)
async def create_api_key(request: Request, key_request: CreateAPIKeyRequest):
    """
    Create a new API key for programmatic access.

    **IMPORTANT**: The full API key is only shown ONCE. Store it securely!

    Requires:
    - Authenticated user (JWT from wallet login)
    - DEN token ownership (verified via tier)

    Example:
        POST /api/auth/api-keys
        Authorization: Bearer <your-jwt>
        {
            "name": "My Agent",
            "expires_days": 90
        }

    Response:
        {
            "api_key": "rob_abc123...",  // SAVE THIS!
            "name": "My Agent",
            "prefix": "rob_abc123",
            "expires_at": "2025-03-05T12:00:00",
            "warning": "Store this key securely - it won't be shown again!"
        }

    Usage:
        curl -H "X-API-KEY: rob_abc123..." https://your-polyrob-host.example/a2a/rpc
    """
    # B45: `hasattr(request.state, 'user_id')` is True whenever ANY middleware
    # touched the attribute — including when it set it to None or "" — so an
    # unauthenticated caller could mint a key owned by a null user (a row no
    # tenant can ever revoke, usable by whoever holds it). The strict policy is
    # the one gate: it also rejects the synthetic `api_user` /
    # `authenticated_api_user` placeholders.
    from api.dependencies import get_user_strict
    user_id = await get_user_strict(request)

    from api.dependencies import optional_container
    container = optional_container()
    api_key_manager = container.get_service('api_key_manager') if container else None

    if not api_key_manager:
        # B2: name the remedy. The key manager is registered only under
        # ENABLE_AUTH (core/initialization.py::initialize_auth_services), which
        # is OFF by default — so "unavailable" on a fresh install is a
        # configuration fact, not a fault.
        raise HTTPException(
            status_code=503,
            detail=("API key minting is not enabled on this instance. "
                    "Set ENABLE_AUTH=true (and configure a database) to turn "
                    "on the account + API-key system, then restart."),
        )

    try:
        result = await api_key_manager.generate_api_key(
            user_id=user_id,
            name=key_request.name,
            expires_days=key_request.expires_days
        )
        return APIKeyResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get("/api-keys", response_model=list[APIKeyInfo])
async def list_api_keys(request: Request):
    """
    List all API keys for the current user.

    Note: Only shows key prefixes, not full keys.
    """
    # B45 (revalidation): the SAME strict gate `create_api_key` uses. Listing
    # and revoking are tenant operations; a null / placeholder identity must
    # not reach another tenant's key rows.
    from api.dependencies import get_user_strict, optional_container
    user_id = await get_user_strict(request)

    container = optional_container()
    api_key_manager = container.get_service('api_key_manager') if container else None

    if not api_key_manager:
        raise HTTPException(
            status_code=503,
            detail=("API keys are not enabled on this instance. "
                    "Set ENABLE_AUTH=true (and configure a database) to turn "
                    "on the account + API-key system, then restart."),
        )

    keys = await api_key_manager.list_user_keys(user_id)
    return [APIKeyInfo(**k) for k in keys]


@router.delete("/api-keys/{key_prefix}")
async def revoke_api_key(request: Request, key_prefix: str):
    """
    Revoke an API key.

    Args:
        key_prefix: The key prefix (e.g., "rob_abc123")
    """
    # B45 (revalidation): same strict gate as create/list — see above.
    from api.dependencies import get_user_strict, optional_container
    user_id = await get_user_strict(request)

    container = optional_container()
    api_key_manager = container.get_service('api_key_manager') if container else None

    if not api_key_manager:
        raise HTTPException(
            status_code=503,
            detail=("API keys are not enabled on this instance. "
                    "Set ENABLE_AUTH=true (and configure a database) to turn "
                    "on the account + API-key system, then restart."),
        )

    success = await api_key_manager.revoke_key(user_id, key_prefix)

    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    return {"success": True, "message": f"API key {key_prefix} revoked"}
