"""Common claim contract for owner/SIWE session JWTs on every public surface."""
import math

import jwt

SESSION_AUDIENCE = "polyrob-session"


def decode_session_token(token, secret, *, verify_exp=True):
    """Require bounded, revocable credentials; callers still check the denylist.

    Both supported minters already emit exp and jti. Older credentials missing
    either must sign in again rather than retain unbounded or irrevocable access.
    """
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"],
            audience=SESSION_AUDIENCE,
            options={"require": ["exp", "jti", "aud"], "verify_exp": verify_exp,
                     "strict_aud": True},
        )
    except (ValueError, OverflowError, TypeError) as exc:
        raise jwt.InvalidTokenError("invalid session claims") from exc
    expiry = claims["exp"]
    try:
        finite_expiry = math.isfinite(expiry)
    except (TypeError, ValueError, OverflowError):
        finite_expiry = False
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not finite_expiry):
        raise jwt.InvalidTokenError("invalid session expiry")
    identifier = claims["jti"]
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128:
        raise jwt.InvalidTokenError("invalid session identifier")
    return claims
