"""Owner username/password login — Posture 1's net-new auth path (design doc §1).

Does NOT require standing up the full SIWE SaaS layer. Verifies against
POLYROB_OWNER_USERNAME + POLYROB_OWNER_PASSWORD_HASH (argon2, never plaintext)
and mints a session cookie on the SAME canonical request.state contract
Workstream C4 defines: user_id/tier/role/payment_method/authenticated. Owner
login sets role="owner", tier="admin", payment_method=None (no billing outside
Posture 2).

Reuses the existing wallet-JWT cookie infra byte-for-byte (same "auth_token"
cookie name/flags api/auth_endpoints.py:226-235 uses, same JWT_SECRET_KEY, same
HS256 algorithm) so webview/server.py's existing auth_middleware JWT-decode
branch (webview/server.py:598-641) needs ZERO changes to accept an owner
session — it already decodes "role"/"tier"/"user_id" generically. That decode
branch now writes request.state via api/auth_state.py::set_auth_state (the C4
SSOT) so an owner token converges on the exact same canonical contract this
module mints.
"""
import hmac
import os
from datetime import datetime, timedelta

import jwt as pyjwt
from argon2 import PasswordHasher
from fastapi import Response

from webview import webgate

_hasher = PasswordHasher()

# Precomputed ONCE at module load: a valid argon2 hash of an arbitrary
# constant. verify_owner_password() runs an argon2 verify against this on
# EVERY call where the username doesn't match, so a bad-username attempt
# costs the same wall-clock time as a bad-password attempt against a real
# user. Without this, a wrong username short-circuits before argon2 ever
# runs, and that near-instant-vs-tens-of-ms gap is a network-observable
# oracle that confirms the owner username (user enumeration).
_DUMMY_HASH = _hasher.hash("polyrob-owner-auth-constant-time-dummy")


def owner_credentials_configured() -> bool:
    return bool(
        os.environ.get("POLYROB_OWNER_USERNAME")
        and os.environ.get("POLYROB_OWNER_PASSWORD_HASH")
    )


def verify_owner_password(username: str, password: str) -> bool:
    """Fail-closed: any misconfiguration or verify failure returns False, never raises.

    Timing-safe by construction: every call — bad username, bad password,
    unconfigured credentials, or a correct pair — performs exactly ONE
    argon2 verify (against the real hash when the username matches, else a
    precomputed dummy hash) before returning. There is no early fast-return,
    so wall-clock time never leaks which field (username vs password) was
    wrong, or whether owner auth is configured at all. The username
    comparison itself uses `hmac.compare_digest` so it isn't a fast
    string-compare oracle either.
    """
    configured = owner_credentials_configured()
    expected_username = os.environ.get("POLYROB_OWNER_USERNAME", "") if configured else ""
    expected_hash = os.environ.get("POLYROB_OWNER_PASSWORD_HASH", "") if configured else ""

    username_matches = configured and hmac.compare_digest(
        username.encode("utf-8"), expected_username.encode("utf-8")
    )
    target_hash = expected_hash if username_matches else _DUMMY_HASH

    try:
        verified = _hasher.verify(target_hash, password)
    except Exception:
        verified = False

    return bool(verified and username_matches)


def issue_owner_session_cookie(response: Response) -> str:
    """Mint + set the owner session JWT. Returns the raw token."""
    jwt_secret = os.environ.get("JWT_SECRET_KEY")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET_KEY not configured - cannot issue owner session")

    expires_at = datetime.utcnow() + timedelta(days=7)
    payload = {
        "sub": webgate.local_owner_id(),
        "user_id": webgate.local_owner_id(),
        "tier": "admin",
        "role": "owner",
        "payment_method": None,
        "iat": datetime.utcnow(),
        "exp": expires_at,
    }
    token = pyjwt.encode(payload, jwt_secret, algorithm="HS256")

    is_production = os.environ.get("ENVIRONMENT", "production") == "production"
    response.set_cookie(
        key="auth_token",
        value=token,
        max_age=7 * 24 * 60 * 60,
        path="/",
        secure=is_production,
        httponly=True,
        samesite="lax",
    )
    return token


__all__ = ["owner_credentials_configured", "verify_owner_password", "issue_owner_session_cookie"]
