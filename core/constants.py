"""Auth-related constants and pure helpers.

Lives in core so agent-side code (modules/, agents/, tools/) can validate
roles/tiers without depending on the api/ package. The api/auth_constants.py
module re-exports from here for backward compatibility with server-side
imports.
"""

from typing import List, Set
import os

# Roles
VALID_ROLES: List[str] = [
    'user',
    'admin',
    'owner',
]

# "owner" = the instance operator authenticating to their own console via
# webview/owner_auth.py (argon2 password-gated). They legitimately ARE an
# admin of their own instance, so "owner" is admin-by-role — this keeps
# owner-login admin-by-role AND admin-by-tier consistent (see H1 fix:
# api/admin_endpoints.py::require_admin previously read a second
# request.state.is_admin truth source that could diverge from this).
# NOTE: "owner" is admin-equivalent for READS (is_admin/require_admin) but is
# NOT admin-assignable — role="owner" may ONLY be minted by the password-gated
# webview/owner_auth.py::issue_owner_session_cookie. See ASSIGNABLE_ROLES below;
# do not add "owner" to it or the admin set_user_role endpoint becomes able to
# mint owners (closed in the H1 review-followup).
ADMIN_ROLES: Set[str] = {'admin', 'owner'}

MANAGEMENT_ROLES: Set[str] = {'admin'}

ROLE_MANAGEMENT_ROLES: Set[str] = {'admin'}

# Roles an admin may ASSIGN to another user via the admin role-management
# endpoint (api/admin_endpoints.py::set_user_role). Deliberately excludes
# "owner": that role is reserved for the password-gated owner-login
# (webview/owner_auth.py::issue_owner_session_cookie) and must never be
# grantable by another admin.
ASSIGNABLE_ROLES: Set[str] = set(VALID_ROLES) - {'owner'}


# Tiers
VALID_TIERS: List[str] = [
    'free',
    'free_access',
    'holder',
    'x402',
    'admin',
]

FULL_ACCESS_TIERS: Set[str] = {'free_access', 'holder', 'x402', 'admin'}

PAID_TIERS: Set[str] = {'free_access', 'holder', 'x402'}


def is_admin_role(role: str) -> bool:
    return role in ADMIN_ROLES


def is_admin_wallet(wallet_address: str) -> bool:
    if not wallet_address:
        return False
    admin_wallets = os.environ.get("ADMIN_WALLETS", "").lower().split(",")
    admin_wallets = [w.strip() for w in admin_wallets if w.strip()]
    return wallet_address.lower() in admin_wallets


def is_admin(role: str = None, wallet_address: str = None) -> bool:
    """Unified admin check: True if admin by role OR by wallet."""
    admin_by_role = is_admin_role(role) if role else False
    admin_by_wallet = is_admin_wallet(wallet_address) if wallet_address else False
    return admin_by_role or admin_by_wallet


def can_manage_users(role: str) -> bool:
    return role in MANAGEMENT_ROLES


def can_change_roles(role: str) -> bool:
    return role in ROLE_MANAGEMENT_ROLES


def extract_admin_info(request_state) -> tuple[bool, str, str]:
    """Extract (is_admin, role, wallet_address) from a request.state-like object.

    Duck-typed: any object with `role`/`wallet_address` attributes works,
    so this stays import-clean of FastAPI.
    """
    role = getattr(request_state, 'role', 'user')
    wallet_address = getattr(request_state, 'wallet_address', None)
    admin_status = is_admin(role=role, wallet_address=wallet_address)
    return admin_status, role, wallet_address


def has_full_access(tier: str) -> bool:
    return tier in FULL_ACCESS_TIERS


def requires_payment(tier: str) -> bool:
    return tier not in {'admin'} and tier in PAID_TIERS


def validate_role(role: str) -> bool:
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {VALID_ROLES}")
    return True


def validate_assignable_role(role: str) -> bool:
    """Validate a role for ADMIN-ASSIGNMENT purposes (e.g. set_user_role).

    Stricter than validate_role(): "owner" is a VALID_ROLE (read-side admin
    checks need it) but must never be assignable by an admin — it is reserved
    for the password-gated owner-login (webview/owner_auth.py).
    """
    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {sorted(ASSIGNABLE_ROLES)}")
    return True


def validate_tier(tier: str) -> bool:
    if tier not in VALID_TIERS:
        raise ValueError(f"Invalid tier '{tier}'. Must be one of: {VALID_TIERS}")
    return True
