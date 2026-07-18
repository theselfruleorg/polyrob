"""Regression: AuthenticationMiddleware._validate_auth must NOT mint an identity
from an arbitrary non-JWT Bearer token.

The original code computed an HMAC of the token and then discarded it, returning
{authenticated: True, user_id: token[:8], permissions: [read, write]} for ANY
non-JWT bearer token — a full auth-bypass / impersonation primitive reachable
whenever API_SECRET/ADMIN_TOKEN is set (which mounts the middleware).

A non-JWT bearer token has no signature to verify against the secret, so it can
only be accepted if it is a real DB-backed API key. Anything else must fail closed.
"""
import pytest

from api.middleware import AuthenticationMiddleware


def _mw():
    # app arg is unused by _validate_auth; a sentinel is fine.
    return AuthenticationMiddleware(app=lambda *a, **k: None, secret_key="unit-test-secret")


@pytest.mark.asyncio
async def test_arbitrary_bearer_token_is_rejected(monkeypatch):
    mw = _mw()

    # No DB available in the unit env → _validate_api_key returns None.
    info = await mw._validate_auth("Bearer totally-made-up-token-not-a-jwt", api_key="")

    assert info is None, (
        "arbitrary non-JWT bearer token must NOT authenticate — this is the "
        "session-hijack bypass"
    )


@pytest.mark.asyncio
async def test_bearer_token_does_not_mint_user_id_prefix():
    mw = _mw()
    token = "abcdef0123456789deadbeefcafebabe0000"  # >=32 chars, not a JWT
    info = await mw._validate_auth(f"Bearer {token}", api_key="")
    # Must never fabricate user_id == token[:8].
    assert info is None or info.get("user_id") != token[:8]
