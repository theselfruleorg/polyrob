"""Server-side auth constants — re-exports from core.constants, plus the ONE
public-path allow-list every HTTP auth gate in ``api/`` shares.

The role/tier definitions live in core/constants.py so agent-side code can
import them without depending on the api/ package. This shim preserves
existing `from api.auth_constants import ...` call sites.

``is_public_path`` is the single answer to "may an anonymous caller reach this
path?".  It exists because ``AuthenticationMiddleware`` and
``fallback_auth_middleware`` each carried their OWN list and they disagreed: the
middleware's list was five EXACT strings, so every documented-public route
(``/api/auth/nonce``, ``/.well-known/agent.json``, ``/api/x402/*``,
``/api/pricing/*``, ``/webhooks/*``) 401'd the moment ``API_SECRET`` /
``ADMIN_TOKEN`` was set (B1, 2026-09-21).
"""

from typing import Tuple

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


#: Paths that are public only as an EXACT match. ``"/"`` MUST never be treated
#: as a prefix — as a ``startswith`` entry it matches every path and disables
#: the whole gate (a total auth bypass).
PUBLIC_PATHS_EXACT: frozenset = frozenset({
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})

#: Path PREFIXES that are public. Every entry here is a surface an anonymous
#: third party must be able to reach for the product to work:
#:   ``/api/auth``      — the wallet login handshake (nonce/verify) that MINTS
#:                        the credential; gating it makes login impossible.
#:   ``/api/x402/``     — pay-per-request challenges + invoice pay; a payer has
#:                        no POLYROB account. Authenticity is cryptographic.
#:   ``/api/pricing``   — published price list (``pricing_endpoints`` is public
#:                        by contract).
#:   ``/webhooks/``     — inbound third-party callbacks; each surface verifies
#:                        its own signature.
#:   ``/.well-known/``  — RFC 8615 discovery (the A2A agent card).
#:   ``/a2a/agent-card``/``/a2a/extended-card`` — the same card on the API path.
#:   ``/eip8004/registration.json`` — the ERC-8004 tokenURI target; an on-chain
#:                        consumer cannot authenticate.
PUBLIC_PATH_PREFIXES: Tuple[str, ...] = (
    "/api/auth",
    "/api/x402/",
    "/api/pricing",
    "/webhooks/",
    "/.well-known/",
    "/a2a/agent-card",
    "/a2a/extended-card",
    "/eip8004/registration.json",
    "/docs",
    "/redoc",
)


#: The role carried by a caller authenticated with the operator SERVICE token
#: (``API_AUTH_TOKEN``). Deliberately NOT in ``ADMIN_ROLES``: the token is a
#: machine-to-machine operator credential, not an admin session, so it must not
#: reach ``/api/admin/*`` (B4). It is sent as ``X-Service-Token``.
SERVICE_ROLE: str = "service"

#: Canonical header for the operator service token. ``X-API-KEY`` still works
#: for ONE release (with a one-time deprecation WARN) so an existing deployment
#: is not broken by the rename.
SERVICE_TOKEN_HEADER: str = "X-Service-Token"


def is_service_role(role: str) -> bool:
    """Whether ``role`` is the operator service-token role."""
    return role == SERVICE_ROLE


def is_public_path(path: str) -> bool:
    """Whether ``path`` may be served without authentication.

    ONE answer, shared by ``AuthenticationMiddleware`` and
    ``fallback_auth_middleware`` — two gates that disagreed about this used to
    401 every documented-public route whenever the first one was enabled.
    """
    if not path:
        return False
    if path in PUBLIC_PATHS_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)
