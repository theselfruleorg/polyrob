"""Server-side auth constants — re-exports from core.constants.

The canonical definitions live in core/constants.py so agent-side code can
import them without depending on the api/ package. This shim preserves
existing `from api.auth_constants import ...` call sites.
"""

from core.constants import (  # noqa: F401
    VALID_ROLES,
    ADMIN_ROLES,
    ASSIGNABLE_ROLES,
    MANAGEMENT_ROLES,
    ROLE_MANAGEMENT_ROLES,
    VALID_TIERS,
    FULL_ACCESS_TIERS,
    PAID_TIERS,
    is_admin_role,
    is_admin_wallet,
    is_admin,
    can_manage_users,
    can_change_roles,
    extract_admin_info,
    has_full_access,
    requires_payment,
    validate_role,
    validate_assignable_role,
    validate_tier,
)
