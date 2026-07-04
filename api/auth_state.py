"""The canonical request.state auth contract (C4 SSOT).

Every middleware that authenticates a request — AuthenticationMiddleware,
JWTAuthMiddleware, X402PaymentMiddleware, and the fallback_auth_middleware in
api/app.py — MUST write these five fields via set_auth_state() instead of
setting attributes ad hoc. verify_payment_for_request and every
Depends(get_user_id) / get_user_strict / get_user_permissive consumer in
api/dependencies.py reads this contract.

Workstream B3 (owner-login session, POLYROB Console posture 1) writes the SAME
contract with role="owner" — this module is what B3 coordinates against.
"""
from typing import Any, Optional


def set_auth_state(
    state: Any,
    *,
    user_id: Optional[str],
    tier: str = "free",
    role: str = "user",
    payment_method: Optional[str] = None,
    authenticated: bool = True,
) -> None:
    """Write the canonical auth-state contract onto `state`.

    `state` is typically a Starlette `Request.state` (a Starlette `State`
    object) but any object that accepts attribute assignment works — this is
    deliberately duck-typed so it's trivial to unit test with a bare
    `types.SimpleNamespace()`.
    """
    state.user_id = user_id
    state.tier = tier
    state.role = role
    state.payment_method = payment_method
    state.authenticated = authenticated
