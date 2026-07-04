"""Free wallet authentication endpoints using SIWE (Sign-In with Ethereum).

Replaces expensive Privy with free industry-standard SIWE.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
import jwt
from datetime import datetime, timedelta
import logging
import os
import re
from api.auth_constants import is_admin_wallet

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

    from core.container import DependencyContainer
    import os

    container = DependencyContainer.get_instance()
    siwe_auth = container.get_service('siwe_authenticator')

    if not siwe_auth:
        raise HTTPException(status_code=500, detail="SIWE authenticator not initialized")

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

    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()

    siwe_auth = container.get_service('siwe_authenticator')
    identity_mapper = container.get_service('identity_mapper')
    db = container.get_service('database_manager')

    if not siwe_auth or not identity_mapper or not db:
        raise HTTPException(status_code=500, detail="Auth services unavailable")

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

    expires_at = datetime.utcnow() + timedelta(days=7)

    # Include admin_wallet flag in token for session-based admin privileges
    # This allows admin wallets to have privileges without modifying the role in DB
    token_payload = {
        "sub": request.wallet_address,      # WALLET AS PRIMARY! ✅
        "user_id": user_id,                 # Internal DB ID
        "chain": request.chain,
        "tier": tier,
        "role": role,
        "admin_wallet": is_admin_by_wallet,  # Session-based admin flag
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
        "is_admin": is_admin_by_wallet or role == 'admin',
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
    max_age = 7 * 24 * 60 * 60  # 7 days

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

    # user_id is added to request.state by middleware
    if not hasattr(request.state, 'user_id'):
        raise HTTPException(status_code=401, detail="Not authenticated")

    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    tier_manager = container.get_service('tier_manager')

    if tier_manager:
        user_info = await tier_manager.get_user_info(request.state.user_id)
        return user_info
    else:
        return {"user_id": request.state.user_id}


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
    if not hasattr(request.state, 'user_id'):
        raise HTTPException(status_code=401, detail="Authentication required. Login with wallet first.")

    user_id = request.state.user_id

    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    api_key_manager = container.get_service('api_key_manager')

    if not api_key_manager:
        raise HTTPException(status_code=503, detail="API key service unavailable")

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
    if not hasattr(request.state, 'user_id'):
        raise HTTPException(status_code=401, detail="Authentication required")

    user_id = request.state.user_id

    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    api_key_manager = container.get_service('api_key_manager')

    if not api_key_manager:
        raise HTTPException(status_code=503, detail="API key service unavailable")

    keys = await api_key_manager.list_user_keys(user_id)
    return [APIKeyInfo(**k) for k in keys]


@router.delete("/api-keys/{key_prefix}")
async def revoke_api_key(request: Request, key_prefix: str):
    """
    Revoke an API key.

    Args:
        key_prefix: The key prefix (e.g., "rob_abc123")
    """
    if not hasattr(request.state, 'user_id'):
        raise HTTPException(status_code=401, detail="Authentication required")

    user_id = request.state.user_id

    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    api_key_manager = container.get_service('api_key_manager')

    if not api_key_manager:
        raise HTTPException(status_code=503, detail="API key service unavailable")

    success = await api_key_manager.revoke_key(user_id, key_prefix)

    if not success:
        raise HTTPException(status_code=404, detail="API key not found")

    return {"success": True, "message": f"API key {key_prefix} revoked"}
