"""Admin management endpoints for user and credit management.

Phase 1 MVP endpoints:
- User search: GET /api/admin/users/search
- Token verification: POST /api/admin/users/{user_id}/verify-token
- User audit trail: GET /api/admin/users/{user_id}/audit
- User sessions: GET /api/admin/users/{user_id}/sessions
- Block/unblock: POST /api/admin/users/{user_id}/block, /unblock
- Enhanced stats: GET /api/admin/stats/dashboard
- Activity feed: GET /api/admin/activity
"""

from fastapi import APIRouter, HTTPException, Request, Depends, Query
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal
import logging
from datetime import datetime, timedelta
from api.auth_constants import (
    VALID_ROLES,
    VALID_TIERS,
    validate_role,
    validate_assignable_role,
    validate_tier,
    extract_admin_info,
)

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str:
    """Get client IP from request, handling proxies."""
    # Check X-Forwarded-For header (set by nginx/proxies)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        return forwarded_for.split(",")[0].strip()
    # Fall back to direct client IP
    return request.client.host if request.client else "unknown"


async def get_audit_logger():
    """Get audit logger instance."""
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        db = container.get_service('database_manager')
        if db:
            from modules.database.audit_log import AuditLogger
            return AuditLogger(db)
    except Exception as e:
        logger.warning(f"Could not get audit logger: {e}")
    return None

router = APIRouter(prefix="/admin", tags=["admin"])


# Request/Response Models

class AddCreditsRequest(BaseModel):
    """Request to add credits to user."""
    amount: int
    reason: str
    transaction_type: str = "admin_grant"


class SetRoleRequest(BaseModel):
    """Request to set user role."""
    role: str


class SetTierRequest(BaseModel):
    """Request to set user tier."""
    tier: str


class UserInfo(BaseModel):
    """User information response."""
    user_id: str
    wallet_address: Optional[str]
    email: Optional[str]
    tier: Optional[str]
    role: str
    den_token_count: int
    balance: int
    lifetime_earned: int
    lifetime_spent: int
    created_at: str


class CreditTransaction(BaseModel):
    """Credit transaction info."""
    amount: int
    transaction_type: str
    reason: str
    balance_before: int
    balance_after: int
    timestamp: str


class BlockUserRequest(BaseModel):
    """Request to block a user."""
    reason: str = Field(..., min_length=1, description="Reason for blocking")


class UserSearchResult(BaseModel):
    """User search result."""
    user_id: str
    wallet_address: Optional[str]
    email: Optional[str]
    tier: Optional[str]
    role: str
    den_token_count: int
    balance: int
    created_at: str
    is_blocked: bool = False


class TokenVerificationResult(BaseModel):
    """Token verification result."""
    previous_count: int
    new_count: int
    previous_tier: str
    new_tier: str
    tier_changed: bool
    token_ids: List[str]
    verified_at: str
    bonuses_granted: int = 0
    bonuses_already_claimed: int = 0


class AuditEvent(BaseModel):
    """Audit event."""
    id: int
    timestamp: str
    event_type: str
    actor_id: Optional[str]
    actor_wallet: Optional[str]
    actor_ip: Optional[str]
    target_id: Optional[str]
    action: str
    old_value: Optional[str]
    new_value: Optional[str]
    success: bool


class SessionUsageSummary(BaseModel):
    """Session usage summary."""
    session_id: str
    total_cost: int
    total_calls: int
    timestamp: str


class UserSessionsResponse(BaseModel):
    """User sessions response."""
    total_sessions: int
    total_spent: int
    sessions: List[SessionUsageSummary]


class DashboardStats(BaseModel):
    """Enhanced dashboard statistics."""
    users: Dict[str, Any]
    credits: Dict[str, Any]
    sessions: Dict[str, Any]
    revenue: Dict[str, Any]
    alerts: List[Dict[str, Any]]


class BlockStatusResponse(BaseModel):
    """Block status response."""
    is_blocked: bool
    blocked_reason: Optional[str]
    blocked_at: Optional[str]
    blocked_by: Optional[str]


class BillingFailure(BaseModel):
    """Billing failure record."""
    id: int
    user_id: str
    session_id: str
    request_id: str
    credits_owed: int
    api_cost_usd: float
    model: Optional[str]
    created_at: str
    resolved_at: Optional[str]
    status: str
    resolution_notes: Optional[str]
    current_balance: Optional[int] = None
    wallet_address: Optional[str] = None


class BillingFailuresResponse(BaseModel):
    """Billing failures list response."""
    failures: List[BillingFailure]
    total: int
    status_filter: str
    total_credits_owed: int
    total_api_cost_usd: float


class ResolveBillingFailureRequest(BaseModel):
    """Request to resolve a billing failure."""
    resolution: Literal["charged", "written_off", "refunded"]
    notes: str = ""


# Dependency: Check admin access

async def require_admin(request: Request):
    """Require admin access for endpoint.

    Reads the SINGLE canonical admin-truth source: `role` (+ `wallet_address`)
    on request.state, via the same extract_admin_info()/is_admin() helper
    verify_payment_for_request uses. Deliberately does NOT read
    request.state.is_admin — that was a second, independently-set truth
    source (some middlewares set it, some didn't) that could diverge from
    `role` (H1 fix: an owner-login session had role="owner", tier="admin" but
    was denied here pre-fix because "owner" wasn't recognized as admin).

    Admin access is granted via:
    1. role in ADMIN_ROLES (currently 'admin' or 'owner')
    2. wallet address in ADMIN_WALLETS env var
    """
    admin_status, _role, _wallet = extract_admin_info(request.state)
    if not admin_status:
        raise HTTPException(
            status_code=403,
            detail="Admin access required"
        )
    return True


# Endpoints

@router.get("/users", dependencies=[Depends(require_admin)])
async def list_users(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    tier: Optional[str] = None,
    role: Optional[str] = None
) -> List[UserInfo]:
    """List all users (admin only).

    Query Parameters:
        limit: Max results (default 50)
        offset: Pagination offset (default 0)
        tier: Filter by tier (optional)
        role: Filter by role (optional)
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Build query with filters
    where_clauses = []
    params = []

    if tier:
        where_clauses.append("u.tier = ?")
        params.append(tier)

    if role:
        where_clauses.append("u.role = ?")
        params.append(role)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    params.extend([limit, offset])

    users = await db.fetch_all(f"""
        SELECT
            u.user_id,
            u.wallet_address,
            u.email,
            u.tier,
            u.role,
            u.den_token_count,
            u.created_at,
            c.balance,
            c.lifetime_earned,
            c.lifetime_spent
        FROM user_profiles u
        LEFT JOIN user_credits c ON u.user_id = c.user_id
        {where_sql}
        ORDER BY u.created_at DESC
        LIMIT ? OFFSET ?
    """, tuple(params))

    return [
        UserInfo(
            user_id=u['user_id'],
            wallet_address=u['wallet_address'],
            email=u['email'],
            tier=u['tier'],
            role=u['role'] or 'user',
            den_token_count=u['den_token_count'] or 0,
            balance=u['balance'] or 0,
            lifetime_earned=u['lifetime_earned'] or 0,
            lifetime_spent=u['lifetime_spent'] or 0,
            created_at=u['created_at']
        )
        for u in users
    ]


@router.get("/users/{user_id}", dependencies=[Depends(require_admin)])
async def get_user(request: Request, user_id: str) -> UserInfo:
    """Get user details (admin only)."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    user = await db.fetch_one("""
        SELECT
            u.user_id,
            u.wallet_address,
            u.email,
            u.tier,
            u.role,
            u.den_token_count,
            u.created_at,
            c.balance,
            c.lifetime_earned,
            c.lifetime_spent
        FROM user_profiles u
        LEFT JOIN user_credits c ON u.user_id = c.user_id
        WHERE u.user_id = ?
    """, (user_id,))

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserInfo(
        user_id=user['user_id'],
        wallet_address=user['wallet_address'],
        email=user['email'],
        tier=user['tier'],
        role=user['role'] or 'user',
        den_token_count=user['den_token_count'] or 0,
        balance=user['balance'] or 0,
        lifetime_earned=user['lifetime_earned'] or 0,
        lifetime_spent=user['lifetime_spent'] or 0,
        created_at=user['created_at']
    )


@router.post("/users/{user_id}/credits/add", dependencies=[Depends(require_admin)])
async def add_user_credits(
    request: Request,
    user_id: str,
    credits: AddCreditsRequest
):
    """Add credits to user account (admin only)."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    balance_mgr = container.get_service('balance_manager')

    if not balance_mgr:
        raise HTTPException(status_code=503, detail="Balance manager unavailable")

    admin_id = getattr(request.state, 'user_id', 'unknown')
    client_ip = get_client_ip(request)

    # Get balance before for audit
    balance_before_info = await balance_mgr.get_balance(user_id)
    balance_before = balance_before_info.get('balance', 0) if balance_before_info else 0

    success = await balance_mgr.add_credits(
        user_id=user_id,
        amount=credits.amount,
        reason=f"{credits.reason} (granted by admin {admin_id})",
        transaction_type=credits.transaction_type
    )

    if not success:
        raise HTTPException(status_code=500, detail="Failed to add credits")

    # Get updated balance
    balance_info = await balance_mgr.get_balance(user_id)
    balance_after = balance_info.get('balance', 0)

    # SECURITY: Audit log credit addition
    audit = await get_audit_logger()
    if audit:
        await audit.log_credit_change(
            admin_id=admin_id,
            target_user_id=user_id,
            amount=credits.amount,
            is_addition=True,
            reason=credits.reason,
            balance_before=balance_before,
            balance_after=balance_after,
            ip_address=client_ip
        )

    return {
        "success": True,
        "user_id": user_id,
        "added_amount": credits.amount,
        "new_balance": balance_after,
        "transaction_type": credits.transaction_type,
        "reason": credits.reason
    }


@router.post("/users/{user_id}/credits/deduct", dependencies=[Depends(require_admin)])
async def deduct_user_credits(
    request: Request,
    user_id: str,
    credits: AddCreditsRequest
):
    """Deduct credits from user account (admin only)."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    balance_mgr = container.get_service('balance_manager')

    if not balance_mgr:
        raise HTTPException(status_code=503, detail="Balance manager unavailable")

    admin_id = getattr(request.state, 'user_id', 'unknown')
    client_ip = get_client_ip(request)

    # Get balance before for audit
    balance_before_info = await balance_mgr.get_balance(user_id)
    balance_before = balance_before_info.get('balance', 0) if balance_before_info else 0

    success = await balance_mgr.deduct_credits(
        user_id=user_id,
        amount=credits.amount,
        reason=f"{credits.reason} (deducted by admin {admin_id})"
    )

    if not success:
        raise HTTPException(status_code=400, detail="Insufficient balance or deduction failed")

    # Get updated balance
    balance_info = await balance_mgr.get_balance(user_id)
    balance_after = balance_info.get('balance', 0)

    # SECURITY: Audit log credit deduction
    audit = await get_audit_logger()
    if audit:
        await audit.log_credit_change(
            admin_id=admin_id,
            target_user_id=user_id,
            amount=credits.amount,
            is_addition=False,
            reason=credits.reason,
            balance_before=balance_before,
            balance_after=balance_after,
            ip_address=client_ip
        )

    return {
        "success": True,
        "user_id": user_id,
        "deducted_amount": credits.amount,
        "new_balance": balance_after
    }


@router.get("/users/{user_id}/credits", dependencies=[Depends(require_admin)])
async def get_user_credits(request: Request, user_id: str):
    """Get user's credit balance and history (admin only)."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    balance_mgr = container.get_service('balance_manager')

    if not balance_mgr:
        raise HTTPException(status_code=503, detail="Balance manager unavailable")

    balance_info = await balance_mgr.get_balance(user_id)
    transactions = await balance_mgr.get_transaction_history(user_id, limit=50)
    monthly_stats = await balance_mgr.get_monthly_stats(user_id)

    return {
        "user_id": user_id,
        "balance": balance_info['balance'],
        "lifetime_earned": balance_info['lifetime_earned'],
        "lifetime_spent": balance_info['lifetime_spent'],
        "month_spent": monthly_stats['month_spent'],
        "month_earned": monthly_stats['month_earned'],
        "recent_transactions": [
            CreditTransaction(
                amount=t['amount'],
                transaction_type=t['transaction_type'],
                reason=t['reason'],
                balance_before=t['balance_before'],
                balance_after=t['balance_after'],
                timestamp=t['timestamp']
            )
            for t in transactions
        ]
    }


@router.post("/users/{user_id}/role", dependencies=[Depends(require_admin)])
async def set_user_role(
    request: Request,
    user_id: str,
    role_request: SetRoleRequest
):
    """Set user role (admin only).

    Assignable roles: user, admin.
    "owner" is NOT assignable here — it is reserved for the password-gated
    owner-login (webview/owner_auth.py::issue_owner_session_cookie) and must
    never be mintable via admin action.
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Validate role using centralized ASSIGNMENT-boundary validation. This is
    # stricter than validate_role(): "owner" is a VALID_ROLE (read-side admin
    # checks need it) but must never be admin-assignable.
    try:
        validate_assignable_role(role_request.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get current role for audit log and verify user exists
    current_user = await db.fetch_one(
        "SELECT role FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    if not current_user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")

    old_role = current_user['role']

    # Update role
    await db.execute("""
        UPDATE user_profiles
        SET role = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (role_request.role, user_id))

    admin_id = getattr(request.state, 'user_id', 'unknown')
    client_ip = get_client_ip(request)

    # SECURITY: Audit log role change
    audit = await get_audit_logger()
    if audit:
        await audit.log_role_change(
            admin_id=admin_id,
            target_user_id=user_id,
            old_role=old_role,
            new_role=role_request.role,
            ip_address=client_ip
        )

    logger.info(f"User {user_id} role changed from {old_role} to {role_request.role} by admin {admin_id}")

    return {
        "success": True,
        "user_id": user_id,
        "new_role": role_request.role
    }


@router.post("/users/{user_id}/tier", dependencies=[Depends(require_admin)])
async def set_user_tier(
    request: Request,
    user_id: str,
    tier_request: SetTierRequest
):
    """Manually set user tier (admin only).

    Tiers:
    - free: No access (blocked from sessions)
    - free_access: Admin-granted access without DEN token (uses credits)
    - holder: DEN token holder (automatic via token verification)
    - x402: Pay-per-request
    - admin: Unlimited access

    Use 'free_access' to grant someone access for demo/showcase purposes.
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Validate tier using centralized validation
    try:
        validate_tier(tier_request.tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get current tier for audit log and verify user exists
    current_user = await db.fetch_one(
        "SELECT tier FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    if not current_user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")

    old_tier = current_user['tier']

    # Update tier
    await db.execute("""
        UPDATE user_profiles
        SET tier = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (tier_request.tier, user_id))

    admin_id = getattr(request.state, 'user_id', 'unknown')
    client_ip = get_client_ip(request)

    # SECURITY: Audit log tier change
    audit = await get_audit_logger()
    if audit:
        await audit.log_tier_change(
            admin_id=admin_id,
            target_user_id=user_id,
            old_tier=old_tier,
            new_tier=tier_request.tier,
            ip_address=client_ip
        )

    logger.info(f"User {user_id} tier changed from {old_tier} to {tier_request.tier} by admin {admin_id}")

    return {
        "success": True,
        "user_id": user_id,
        "new_tier": tier_request.tier
    }


@router.get("/stats", dependencies=[Depends(require_admin)])
async def get_system_stats(request: Request):
    """Get system statistics (admin only)."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # User stats
    total_users = await db.fetch_one("SELECT COUNT(*) as count FROM user_profiles")
    users_by_tier = await db.fetch_all("""
        SELECT tier, COUNT(*) as count
        FROM user_profiles
        GROUP BY tier
    """)
    users_by_role = await db.fetch_all("""
        SELECT role, COUNT(*) as count
        FROM user_profiles
        GROUP BY role
    """)

    # Credit stats
    total_credits_issued = await db.fetch_one("""
        SELECT COALESCE(SUM(balance), 0) as total
        FROM user_credits
    """)
    total_credits_spent = await db.fetch_one("""
        SELECT COALESCE(SUM(ABS(amount)), 0) as total
        FROM credit_transactions
        WHERE transaction_type = 'usage'
    """)

    # x402 stats (if enabled)
    x402_payments = await db.fetch_one("""
        SELECT
            COUNT(*) as total_requests,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) as completed,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN amount_usd ELSE 0 END), 0) as total_revenue
        FROM x402_payment_requests
    """)

    return {
        "users": {
            "total": total_users['count'] if total_users else 0,
            "by_tier": {row['tier']: row['count'] for row in users_by_tier},
            "by_role": {row['role'] or 'user': row['count'] for row in users_by_role}
        },
        "credits": {
            "total_issued": total_credits_issued['total'] if total_credits_issued else 0,
            "total_spent": total_credits_spent['total'] if total_credits_spent else 0
        },
        "x402": {
            "total_requests": x402_payments['total_requests'] if x402_payments else 0,
            "completed_payments": x402_payments['completed'] if x402_payments else 0,
            "total_revenue_usd": x402_payments['total_revenue'] if x402_payments else 0
        }
    }


@router.get("/users/search", dependencies=[Depends(require_admin)])
async def search_users(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    field: Literal["wallet", "email", "user_id", "all"] = Query("all", description="Field to search"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
) -> List[UserSearchResult]:
    """Search users by wallet address, email, or user_id.

    - Wallet search is case-insensitive and supports partial match
    - Email search is case-insensitive and supports partial match
    - User ID search is exact match prefix
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Build search query based on field
    search_term = q.lower().strip()
    where_clauses = []
    params = []

    if field == "wallet" or field == "all":
        where_clauses.append("LOWER(u.wallet_address) LIKE ?")
        params.append(f"%{search_term}%")

    if field == "email" or field == "all":
        where_clauses.append("LOWER(u.email) LIKE ?")
        params.append(f"%{search_term}%")

    if field == "user_id" or field == "all":
        where_clauses.append("u.user_id LIKE ?")
        params.append(f"{search_term}%")

    where_sql = f"WHERE ({' OR '.join(where_clauses)})"
    params.extend([limit, offset])

    users = await db.fetch_all(f"""
        SELECT
            u.user_id,
            u.wallet_address,
            u.email,
            u.tier,
            u.role,
            u.den_token_count,
            u.created_at,
            COALESCE(c.balance, 0) as balance,
            COALESCE(b.is_blocked, 0) as is_blocked
        FROM user_profiles u
        LEFT JOIN user_credits c ON u.user_id = c.user_id
        LEFT JOIN blocked_users b ON u.user_id = b.user_id
        {where_sql}
        ORDER BY u.created_at DESC
        LIMIT ? OFFSET ?
    """, tuple(params))

    return [
        UserSearchResult(
            user_id=u['user_id'],
            wallet_address=u['wallet_address'],
            email=u['email'],
            tier=u['tier'],
            role=u['role'] or 'user',
            den_token_count=u['den_token_count'] or 0,
            balance=u['balance'] or 0,
            created_at=str(u['created_at']),
            is_blocked=bool(u['is_blocked'])
        )
        for u in users
    ]


@router.post("/users/{user_id}/verify-token", dependencies=[Depends(require_admin)])
async def verify_user_token(
    request: Request,
    user_id: str
) -> TokenVerificationResult:
    """Re-verify DEN token ownership for a user and update tier if needed.

    Uses AlchemyTool to check current token ownership and:
    1. Updates den_token_count and den_token_verified_at
    2. Upgrades tier to 'holder' if user has tokens and is currently 'free'
    3. Grants DEN Sign Up Allowance for any unclaimed token IDs
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Get user info
    user = await db.fetch_one("""
        SELECT user_id, wallet_address, tier, den_token_count
        FROM user_profiles WHERE user_id = ?
    """, (user_id,))

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    wallet_address = user['wallet_address']
    previous_tier = user['tier']
    previous_count = user['den_token_count'] or 0

    # Try to get AlchemyTool from container services
    alchemy_tool = container.get_service('alchemy_tool')
    if not alchemy_tool:
        # Try to get 'alchemy' service (the registered name)
        alchemy_tool = container.get_service('alchemy')

    if not alchemy_tool:
        # Try to create one on the fly as last resort
        try:
            from tools.alchemy.alchemy_tool import AlchemyTool
            from core.config import BotConfig
            config = BotConfig()  # BaseSettings auto-loads from env
            alchemy_tool = AlchemyTool(name="alchemy", config=config, container=container)
            await alchemy_tool.initialize()
        except Exception as e:
            logger.error(f"Could not initialize AlchemyTool: {e}")
            raise HTTPException(
                status_code=503,
                detail=f"Token verification service unavailable: {e}"
            )

    # Call Alchemy to verify token
    try:
        from tools.alchemy.alchemy_tool import CheckTokenParams
        result = await alchemy_tool.alchemy_check_token(
            CheckTokenParams(address=wallet_address)
        )
    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        raise HTTPException(status_code=500, detail=f"Token verification failed: {e}")

    if result.get('status') != 'success':
        raise HTTPException(
            status_code=500,
            detail=result.get('message', 'Token verification failed')
        )

    # Extract results
    new_count = result.get('token_count', 0)
    token_ids = result.get('token_ids', [])
    verified_at = datetime.utcnow().isoformat() + "Z"

    # Determine new tier
    new_tier = previous_tier
    tier_changed = False

    # Only auto-update tier between 'free' and 'holder'
    # Preserve admin-granted tiers: free_access, x402, admin
    if new_count > 0 and previous_tier == 'free':
        new_tier = 'holder'
        tier_changed = True
    elif new_count == 0 and previous_tier == 'holder':
        # Only downgrade from holder to free
        # (Don't touch free_access, x402, or admin)
        new_tier = 'free'
        tier_changed = True

    # Update user profile
    await db.execute("""
        UPDATE user_profiles
        SET den_token_count = ?,
            den_token_verified_at = CURRENT_TIMESTAMP,
            tier = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (new_count, new_tier, user_id))

    # Check and grant bonuses for new token IDs
    bonuses_granted = 0
    bonuses_already_claimed = 0

    if token_ids:
        # Get contract address from config
        from core.config import BotConfig
        config = BotConfig()  # BaseSettings auto-loads from env
        contract_address = getattr(config, 'den_token_contract_address', '')

        if contract_address:
            for token_id in token_ids:
                # Check if bonus already granted for this token
                existing = await db.fetch_one("""
                    SELECT 1 FROM den_token_bonuses
                    WHERE token_id = ? AND contract_address = ?
                """, (token_id, contract_address))

                if existing:
                    bonuses_already_claimed += 1
                else:
                    # Grant bonus
                    from modules.credits.pricing import DEN_SIGNUP_ALLOWANCE
                    balance_mgr = container.get_service('balance_manager')
                    if balance_mgr:
                        await balance_mgr.add_credits(
                            user_id=user_id,
                            amount=DEN_SIGNUP_ALLOWANCE,
                            reason=f"DEN Sign Up Allowance for token #{token_id}",
                            transaction_type="den_bonus"
                        )
                        # Record bonus granted
                        await db.execute("""
                            INSERT INTO den_token_bonuses (token_id, contract_address)
                            VALUES (?, ?)
                        """, (token_id, contract_address))
                        bonuses_granted += 1

    # Audit log
    admin_id = getattr(request.state, 'user_id', 'unknown')
    client_ip = get_client_ip(request)
    audit = await get_audit_logger()

    if audit and tier_changed:
        await audit.log_tier_change(
            admin_id=admin_id,
            target_user_id=user_id,
            old_tier=previous_tier,
            new_tier=new_tier,
            ip_address=client_ip
        )

    return TokenVerificationResult(
        previous_count=previous_count,
        new_count=new_count,
        previous_tier=previous_tier,
        new_tier=new_tier,
        tier_changed=tier_changed,
        token_ids=token_ids,
        verified_at=verified_at,
        bonuses_granted=bonuses_granted,
        bonuses_already_claimed=bonuses_already_claimed
    )


@router.get("/users/{user_id}/audit", dependencies=[Depends(require_admin)])
async def get_user_audit_trail(
    request: Request,
    user_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0)
) -> List[AuditEvent]:
    """Get audit trail for a specific user."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Check user exists
    user = await db.fetch_one(
        "SELECT 1 FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get audit events where user is either actor or target
    events = await db.fetch_all("""
        SELECT
            id, timestamp, event_type, actor_id, actor_wallet, actor_ip,
            target_id, action, old_value, new_value, success
        FROM audit_log
        WHERE actor_id = ? OR target_id = ?
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """, (user_id, user_id, limit, offset))

    return [
        AuditEvent(
            id=e['id'],
            timestamp=str(e['timestamp']),
            event_type=e['event_type'],
            actor_id=e['actor_id'],
            actor_wallet=e['actor_wallet'],
            actor_ip=e['actor_ip'],
            target_id=e['target_id'],
            action=e['action'],
            old_value=e['old_value'],
            new_value=e['new_value'],
            success=bool(e['success'])
        )
        for e in events
    ]


@router.get("/users/{user_id}/sessions", dependencies=[Depends(require_admin)])
async def get_user_sessions(
    request: Request,
    user_id: str,
    limit: int = Query(50, ge=1, le=200)
) -> UserSessionsResponse:
    """Get user's session usage summary."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Check user exists
    user = await db.fetch_one(
        "SELECT 1 FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get session summaries from usage_records
    sessions = await db.fetch_all("""
        SELECT
            session_id,
            SUM(cost) as total_cost,
            COUNT(*) as total_calls,
            MAX(timestamp) as last_timestamp
        FROM usage_records
        WHERE user_id = ?
        GROUP BY session_id
        ORDER BY last_timestamp DESC
        LIMIT ?
    """, (user_id, limit))

    # Get total stats
    totals = await db.fetch_one("""
        SELECT
            COUNT(DISTINCT session_id) as total_sessions,
            COALESCE(SUM(cost), 0) as total_spent
        FROM usage_records
        WHERE user_id = ?
    """, (user_id,))

    return UserSessionsResponse(
        total_sessions=totals['total_sessions'] if totals else 0,
        total_spent=totals['total_spent'] if totals else 0,
        sessions=[
            SessionUsageSummary(
                session_id=s['session_id'],
                total_cost=s['total_cost'] or 0,
                total_calls=s['total_calls'] or 0,
                timestamp=str(s['last_timestamp'])
            )
            for s in sessions
        ]
    )


@router.post("/users/{user_id}/block", dependencies=[Depends(require_admin)])
async def block_user(
    request: Request,
    user_id: str,
    block_request: BlockUserRequest
):
    """Block a user."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Check user exists
    user = await db.fetch_one(
        "SELECT 1 FROM user_profiles WHERE user_id = ?", (user_id,)
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    admin_id = getattr(request.state, 'user_id', 'unknown')

    # Insert or update blocked_users record
    await db.execute("""
        INSERT INTO blocked_users (user_id, is_blocked, blocked_reason, blocked_at, blocked_by)
        VALUES (?, 1, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            is_blocked = 1,
            blocked_reason = excluded.blocked_reason,
            blocked_at = CURRENT_TIMESTAMP,
            blocked_by = excluded.blocked_by,
            unblocked_at = NULL
    """, (user_id, block_request.reason, admin_id))

    # Audit log
    client_ip = get_client_ip(request)
    audit = await get_audit_logger()
    if audit:
        await audit.log(
            event_type="user_blocked",
            action=f"User blocked: {block_request.reason}",
            actor_id=admin_id,
            actor_ip=client_ip,
            target_id=user_id,
            target_type="user",
            metadata={"reason": block_request.reason}
        )

    return {
        "success": True,
        "user_id": user_id,
        "blocked_at": datetime.utcnow().isoformat() + "Z",
        "blocked_by": admin_id
    }


@router.post("/users/{user_id}/unblock", dependencies=[Depends(require_admin)])
async def unblock_user(
    request: Request,
    user_id: str
):
    """Unblock a user."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Check user exists and is blocked
    blocked = await db.fetch_one("""
        SELECT is_blocked FROM blocked_users WHERE user_id = ?
    """, (user_id,))

    if not blocked or not blocked['is_blocked']:
        raise HTTPException(status_code=400, detail="User is not blocked")

    admin_id = getattr(request.state, 'user_id', 'unknown')

    # Update blocked status
    await db.execute("""
        UPDATE blocked_users
        SET is_blocked = 0, unblocked_at = CURRENT_TIMESTAMP
        WHERE user_id = ?
    """, (user_id,))

    # Audit log
    client_ip = get_client_ip(request)
    audit = await get_audit_logger()
    if audit:
        await audit.log(
            event_type="user_unblocked",
            action="User unblocked",
            actor_id=admin_id,
            actor_ip=client_ip,
            target_id=user_id,
            target_type="user"
        )

    return {
        "success": True,
        "user_id": user_id,
        "unblocked_at": datetime.utcnow().isoformat() + "Z"
    }


@router.get("/users/{user_id}/block-status", dependencies=[Depends(require_admin)])
async def get_block_status(
    request: Request,
    user_id: str
) -> BlockStatusResponse:
    """Get user's block status."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Get block status
    blocked = await db.fetch_one("""
        SELECT is_blocked, blocked_reason, blocked_at, blocked_by
        FROM blocked_users WHERE user_id = ?
    """, (user_id,))

    if not blocked:
        return BlockStatusResponse(
            is_blocked=False,
            blocked_reason=None,
            blocked_at=None,
            blocked_by=None
        )

    return BlockStatusResponse(
        is_blocked=bool(blocked['is_blocked']),
        blocked_reason=blocked['blocked_reason'],
        blocked_at=str(blocked['blocked_at']) if blocked['blocked_at'] else None,
        blocked_by=blocked['blocked_by']
    )


@router.get("/stats/dashboard", dependencies=[Depends(require_admin)])
async def get_dashboard_stats(request: Request) -> DashboardStats:
    """Get enhanced dashboard statistics with alerts."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # User stats
    total_users = await db.fetch_one("SELECT COUNT(*) as count FROM user_profiles")
    new_today = await db.fetch_one("""
        SELECT COUNT(*) as count FROM user_profiles
        WHERE DATE(created_at) = DATE('now')
    """)
    users_by_tier = await db.fetch_all("""
        SELECT tier, COUNT(*) as count FROM user_profiles GROUP BY tier
    """)
    users_by_role = await db.fetch_all("""
        SELECT role, COUNT(*) as count FROM user_profiles GROUP BY role
    """)

    # Credit stats
    credit_totals = await db.fetch_one("""
        SELECT
            COALESCE(SUM(balance), 0) as total_balance,
            COALESCE(SUM(lifetime_earned), 0) as total_earned,
            COALESCE(SUM(lifetime_spent), 0) as total_spent
        FROM user_credits
    """)

    # Session stats (today)
    sessions_today = await db.fetch_one("""
        SELECT COUNT(DISTINCT session_id) as count
        FROM usage_records
        WHERE DATE(timestamp) = DATE('now')
    """)

    # Revenue stats - ALL TIME totals
    x402_total = await db.fetch_one("""
        SELECT COALESCE(SUM(amount_usd), 0) as total
        FROM x402_payment_requests
        WHERE status = 'completed'
    """)
    crypto_total = await db.fetch_one("""
        SELECT COALESCE(SUM(amount_usd), 0) as total
        FROM crypto_payments
        WHERE status = 'confirmed'
    """)
    
    # Revenue stats - MTD (Month-to-Date)
    x402_mtd = await db.fetch_one("""
        SELECT COALESCE(SUM(amount_usd), 0) as total
        FROM x402_payment_requests
        WHERE status = 'completed'
        AND strftime('%Y-%m', completed_at) = strftime('%Y-%m', 'now')
    """)
    crypto_mtd = await db.fetch_one("""
        SELECT COALESCE(SUM(amount_usd), 0) as total
        FROM crypto_payments
        WHERE status = 'confirmed'
        AND strftime('%Y-%m', detected_at) = strftime('%Y-%m', 'now')
    """)

    # Alerts
    alerts = []

    # Check for outdated token verifications (>24h)
    outdated_verifications = await db.fetch_one("""
        SELECT COUNT(*) as count FROM user_profiles
        WHERE den_token_count > 0
        AND (den_token_verified_at IS NULL
             OR den_token_verified_at < datetime('now', '-24 hours'))
    """)
    if outdated_verifications and outdated_verifications['count'] > 0:
        alerts.append({
            "type": "outdated_verification",
            "severity": "warning",
            "count": outdated_verifications['count'],
            "message": f"{outdated_verifications['count']} users with outdated token verification (>24h)"
        })

    # Check for pending sweeps (>1h)
    pending_sweeps = await db.fetch_one("""
        SELECT COUNT(*) as count FROM crypto_payments
        WHERE swept_at IS NULL
        AND detected_at < datetime('now', '-1 hour')
    """)
    if pending_sweeps and pending_sweeps['count'] > 0:
        alerts.append({
            "type": "pending_sweep",
            "severity": "warning",
            "count": pending_sweeps['count'],
            "message": f"{pending_sweeps['count']} pending deposits not swept (>1h old)"
        })

    # Check for recent auth failures
    auth_failures = await db.fetch_one("""
        SELECT COUNT(*) as count FROM audit_log
        WHERE event_type = 'auth_failure'
        AND timestamp > datetime('now', '-1 hour')
    """)
    if auth_failures and auth_failures['count'] > 5:
        alerts.append({
            "type": "auth_failures",
            "severity": "high",
            "count": auth_failures['count'],
            "message": f"{auth_failures['count']} authentication failures in the last hour"
        })

    return DashboardStats(
        users={
            "total": total_users['count'] if total_users else 0,
            "new_today": new_today['count'] if new_today else 0,
            "by_tier": {row['tier'] or 'free': row['count'] for row in users_by_tier},
            "by_role": {row['role'] or 'user': row['count'] for row in users_by_role}
        },
        credits={
            "total_balance": credit_totals['total_balance'] if credit_totals else 0,
            "total_issued": credit_totals['total_earned'] if credit_totals else 0,
            "total_spent": credit_totals['total_spent'] if credit_totals else 0
        },
        sessions={
            "total_today": sessions_today['count'] if sessions_today else 0
        },
        revenue={
            # MTD values (shown in breakdown)
            "x402_mtd_usd": float(x402_mtd['total']) if x402_mtd else 0.0,
            "crypto_mtd_usd": float(crypto_mtd['total']) if crypto_mtd else 0.0,
            "mtd_usd": (float(x402_mtd['total']) if x402_mtd else 0.0) + (float(crypto_mtd['total']) if crypto_mtd else 0.0),
            # All-time totals
            "x402_total_usd": float(x402_total['total']) if x402_total else 0.0,
            "crypto_total_usd": float(crypto_total['total']) if crypto_total else 0.0,
            "total_usd": (float(x402_total['total']) if x402_total else 0.0) + (float(crypto_total['total']) if crypto_total else 0.0)
        },
        alerts=alerts
    )


@router.get("/activity", dependencies=[Depends(require_admin)])
async def get_activity_feed(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    actor_id: Optional[str] = Query(None, description="Filter by actor user ID"),
    target_id: Optional[str] = Query(None, description="Filter by target user ID")
) -> List[AuditEvent]:
    """Get recent activity feed from audit log."""
    audit = await get_audit_logger()

    if not audit:
        raise HTTPException(status_code=503, detail="Audit service unavailable")

    events = await audit.get_recent_events(
        limit=limit,
        offset=offset,
        event_type=event_type,
        actor_id=actor_id,
        target_id=target_id
    )

    return [
        AuditEvent(
            id=e['id'],
            timestamp=str(e['timestamp']),
            event_type=e['event_type'],
            actor_id=e['actor_id'],
            actor_wallet=e['actor_wallet'],
            actor_ip=e['actor_ip'],
            target_id=e['target_id'],
            action=e['action'],
            old_value=e['old_value'],
            new_value=e['new_value'],
            success=bool(e['success'])
        )
        for e in events
    ]


@router.get("/billing-failures", dependencies=[Depends(require_admin)])
async def get_billing_failures(
    request: Request,
    status: str = Query("pending", description="Filter by status: pending, resolved, written_off"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0)
) -> BillingFailuresResponse:
    """Get billing failures for admin review.

    Returns list of billing failures where credits could not be deducted.
    Admins can review and resolve these issues.
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Get failures with user info
    failures = await db.fetch_all("""
        SELECT
            bf.id, bf.user_id, bf.session_id, bf.request_id,
            bf.credits_owed, bf.api_cost_usd, bf.model,
            bf.created_at, bf.resolved_at, bf.status, bf.resolution_notes,
            COALESCE(uc.balance, 0) as current_balance,
            u.wallet_address
        FROM billing_failures bf
        LEFT JOIN user_credits uc ON bf.user_id = uc.user_id
        LEFT JOIN user_profiles u ON bf.user_id = u.user_id
        WHERE bf.status = ?
        ORDER BY bf.created_at DESC
        LIMIT ? OFFSET ?
    """, (status, limit, offset))

    # Get totals
    totals = await db.fetch_one("""
        SELECT
            COUNT(*) as total,
            COALESCE(SUM(credits_owed), 0) as total_credits,
            COALESCE(SUM(api_cost_usd), 0) as total_api_cost
        FROM billing_failures
        WHERE status = ?
    """, (status,))

    return BillingFailuresResponse(
        failures=[
            BillingFailure(
                id=f['id'],
                user_id=f['user_id'],
                session_id=f['session_id'],
                request_id=f['request_id'],
                credits_owed=f['credits_owed'],
                api_cost_usd=f['api_cost_usd'],
                model=f['model'],
                created_at=str(f['created_at']),
                resolved_at=str(f['resolved_at']) if f['resolved_at'] else None,
                status=f['status'],
                resolution_notes=f['resolution_notes'],
                current_balance=f['current_balance'],
                wallet_address=f['wallet_address']
            )
            for f in failures
        ],
        total=totals['total'] if totals else 0,
        status_filter=status,
        total_credits_owed=totals['total_credits'] if totals else 0,
        total_api_cost_usd=float(totals['total_api_cost']) if totals else 0.0
    )


@router.post("/billing-failures/{failure_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_billing_failure(
    request: Request,
    failure_id: int,
    resolution_request: ResolveBillingFailureRequest
):
    """Resolve a billing failure.

    Resolution options:
    - charged: Successfully deducted credits from user
    - written_off: Loss absorbed, user not charged
    - refunded: Credits returned to user (if already charged)
    """
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Verify failure exists and is pending
    failure = await db.fetch_one("""
        SELECT id, user_id, credits_owed, status
        FROM billing_failures WHERE id = ?
    """, (failure_id,))

    if not failure:
        raise HTTPException(status_code=404, detail="Billing failure not found")

    if failure['status'] != 'pending':
        raise HTTPException(
            status_code=400,
            detail=f"Failure already resolved with status: {failure['status']}"
        )

    admin_id = getattr(request.state, 'user_id', 'unknown')
    resolution = resolution_request.resolution
    notes = resolution_request.notes

    # If resolution is "charged", try to actually deduct credits
    if resolution == "charged":
        balance_mgr = container.get_service('balance_manager')
        if balance_mgr:
            success = await balance_mgr.deduct_credits(
                user_id=failure['user_id'],
                amount=failure['credits_owed'],
                reason=f"Billing reconciliation (resolved by admin {admin_id})"
            )
            if not success:
                raise HTTPException(
                    status_code=400,
                    detail="Failed to deduct credits - user may still have insufficient balance"
                )

    # Update failure record
    await db.execute("""
        UPDATE billing_failures
        SET status = 'resolved',
            resolved_at = CURRENT_TIMESTAMP,
            resolution_notes = ?
        WHERE id = ?
    """, (f"{resolution}: {notes} (by {admin_id})", failure_id))

    # Audit log
    audit = await get_audit_logger()
    if audit:
        client_ip = get_client_ip(request)
        await audit.log(
            event_type="billing_failure_resolved",
            action=f"Resolved billing failure #{failure_id}: {resolution}",
            actor_id=admin_id,
            actor_ip=client_ip,
            target_id=failure['user_id'],
            target_type="billing_failure",
            old_value="pending",
            new_value=resolution,
            metadata={
                "failure_id": failure_id,
                "credits_owed": failure['credits_owed'],
                "resolution": resolution,
                "notes": notes
            }
        )

    return {
        "success": True,
        "failure_id": failure_id,
        "resolution": resolution,
        "resolved_by": admin_id
    }


@router.get("/billing-failures/summary", dependencies=[Depends(require_admin)])
async def get_billing_failures_summary(request: Request):
    """Get summary of billing failures by status."""
    from core.container import DependencyContainer
    container = DependencyContainer.get_instance()
    db = container.get_service('database_manager')

    if not db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    summary = await db.fetch_all("""
        SELECT
            status,
            COUNT(*) as count,
            COALESCE(SUM(credits_owed), 0) as total_credits,
            COALESCE(SUM(api_cost_usd), 0) as total_api_cost
        FROM billing_failures
        GROUP BY status
    """)

    # Get recent failures (last 24h)
    recent = await db.fetch_one("""
        SELECT COUNT(*) as count
        FROM billing_failures
        WHERE created_at > datetime('now', '-24 hours')
    """)

    result = {
        "by_status": {
            row['status']: {
                "count": row['count'],
                "total_credits_owed": row['total_credits'],
                "total_api_cost_usd": float(row['total_api_cost'])
            }
            for row in summary
        },
        "recent_24h": recent['count'] if recent else 0
    }

    # Calculate totals
    result["total_pending"] = result["by_status"].get("pending", {}).get("count", 0)
    result["total_pending_credits"] = result["by_status"].get("pending", {}).get("total_credits_owed", 0)
    result["total_pending_api_cost"] = result["by_status"].get("pending", {}).get("total_api_cost_usd", 0.0)

    return result
