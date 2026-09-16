"""043 W5 — the owner session cookie is short-lived AND revocable.

Before W5 the owner-login cookie (webview/owner_auth.py) and the wallet/SIWE
cookie (api/auth_endpoints.py) were both 7-day stateless JWTs with no server-side
revocation: ``/logout`` deleted the browser's copy, but a stolen copy stayed
valid for a full week regardless. W5 gives every token a random ``jti`` claim,
shortens the lifetime to ``OWNER_COOKIE_TTL_SECONDS`` (≤24h, one SSOT both
minters import so they cannot drift), and lets ``/logout`` revoke the exact token
via a durable denylist the auth middleware checks.

This module proves: (1) the minted cookie's ``exp`` is ≤24h; (2) ``/logout``'s
``revoke_cookie_token`` puts the ``jti`` on the denylist and the middleware helper
then rejects that token; (3) both minters agree on the lifetime and neither still
ships the old 7-day cookie. W12 (2FA) is deliberately out of scope.
"""
import os
import pathlib
import time
import uuid
from datetime import datetime, timedelta

import jwt as pyjwt
import pytest
from fastapi import Response

from core.token_denylist import (
    OWNER_COOKIE_TTL_SECONDS,
    _INSTANCES,
    get_token_denylist,
    jti_is_revoked,
    revoke_cookie_token,
)

_DAY = 24 * 60 * 60
_REPO = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_denylist(monkeypatch, tmp_path):
    """Throwaway denylist db + a known JWT secret; reset the process-wide
    singleton so revocations never touch the real data home or leak across tests.
    """
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "token_denylist.db"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-owner-cookie-lifetime")
    _INSTANCES.clear()
    yield
    _INSTANCES.clear()


def _decode(token: str) -> dict:
    return pyjwt.decode(token, os.environ["JWT_SECRET_KEY"], algorithms=["HS256"])


# --- lifetime ------------------------------------------------------------------


def test_shared_ttl_is_positive_and_at_most_24h():
    assert 0 < OWNER_COOKIE_TTL_SECONDS <= _DAY


def test_owner_cookie_exp_within_24h_and_carries_a_jti():
    from webview import owner_auth

    resp = Response()
    token = owner_auth.issue_owner_session_cookie(resp)
    claims = _decode(token)

    assert claims.get("jti"), "the minted owner token must carry a jti"
    span = int(claims["exp"]) - int(claims["iat"])
    assert span <= _DAY, f"owner cookie lifetime {span}s exceeds 24h"
    # the Set-Cookie max-age agrees with the token lifetime
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"Max-Age={OWNER_COOKIE_TTL_SECONDS}" in set_cookie


def test_both_minters_agree_on_lifetime():
    import api.auth_endpoints as ae
    import core.token_denylist as td
    import webview.owner_auth as oa

    assert (
        oa.OWNER_COOKIE_TTL_SECONDS
        == ae.OWNER_COOKIE_TTL_SECONDS
        == td.OWNER_COOKIE_TTL_SECONDS
    )


def test_neither_minter_ships_the_old_7_day_cookie():
    for rel in ("webview/owner_auth.py", "api/auth_endpoints.py"):
        src = (_REPO / rel).read_text(encoding="utf-8")
        assert "OWNER_COOKIE_TTL_SECONDS" in src, f"{rel} lost the shared TTL"
        assert '"jti"' in src, f"{rel} no longer mints a jti"
        assert "timedelta(days=7)" not in src, f"{rel} still mints a 7-day cookie"


# --- revocation ----------------------------------------------------------------


def test_logout_revokes_the_exact_token():
    from webview import owner_auth

    resp = Response()
    token = owner_auth.issue_owner_session_cookie(resp)
    jti = _decode(token)["jti"]

    assert get_token_denylist().is_revoked(jti) is False
    revoke_cookie_token(token)  # exactly what /logout calls
    assert get_token_denylist().is_revoked(jti) is True


def test_middleware_helper_rejects_a_revoked_jti():
    import webview.server as server

    jti = uuid.uuid4().hex
    assert server._jti_revoked(jti) is False

    get_token_denylist().revoke(jti, expires_at=time.time() + _DAY)
    assert server._jti_revoked(jti) is True

    # a different token is still admitted; an absent jti is never "revoked"
    assert server._jti_revoked(uuid.uuid4().hex) is False
    assert server._jti_revoked(None) is False
    assert server._jti_revoked("") is False


def test_revoke_cookie_token_is_a_noop_on_junk():
    # a malformed / secret-less / empty token must not raise and must revoke nothing
    revoke_cookie_token(None)
    revoke_cookie_token("")
    revoke_cookie_token("not-a-jwt")
    # a token signed with a different secret decodes to nothing revocable
    other = pyjwt.encode({"jti": "ghost"}, "some-other-secret", algorithm="HS256")
    revoke_cookie_token(other)
    assert get_token_denylist().is_revoked("ghost") is False


def test_expired_rows_are_pruned_on_write():
    dl = get_token_denylist()
    dl.revoke("stale", expires_at=time.time() - 10)  # already expired
    # a fresh revoke triggers _prune, dropping the stale row
    dl.revoke("fresh", expires_at=time.time() + _DAY)
    assert dl.is_revoked("fresh") is True
    assert dl.is_revoked("stale") is False


def test_shared_predicate_reads_the_jti_claim():
    jti = uuid.uuid4().hex
    assert jti_is_revoked({"jti": jti}) is False
    assert jti_is_revoked(None) is False
    assert jti_is_revoked({}) is False
    get_token_denylist().revoke(jti, expires_at=time.time() + _DAY)
    assert jti_is_revoked({"jti": jti}) is True


# --- the api-service gate (I2) actually refuses a revoked token -----------------


def _bearer_request(path: str, token: str):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "state": {},
    }
    return Request(scope)


async def _ok(_request):
    from starlette.responses import Response as SResp

    return SResp("ok", status_code=200)


def _mint_api_token(secret: str, *, jti: str, ttl_seconds: int = 3600) -> str:
    return pyjwt.encode(
        {
            "sub": "0xabc",
            "user_id": "u",
            "role": "owner",
            "tier": "admin",
            "jti": jti,
            "exp": datetime.utcnow() + timedelta(seconds=ttl_seconds),
        },
        secret,
        algorithm="HS256",
    )


@pytest.mark.asyncio
async def test_api_jwt_gate_admits_then_refuses_after_logout():
    from api.jwt_middleware import JWTAuthMiddleware

    secret = os.environ["JWT_SECRET_KEY"]
    mw = JWTAuthMiddleware(app=None, jwt_secret=secret)
    jti = uuid.uuid4().hex
    token = _mint_api_token(secret, jti=jti)

    # before /logout: the standalone api gate lets the token through
    resp = await mw.dispatch(_bearer_request("/api/x", token), _ok)
    assert resp.status_code == 200

    # after /logout revokes the jti: the SAME gate refuses it (401)
    get_token_denylist().revoke(jti, expires_at=time.time() + _DAY)
    denied = await mw.dispatch(_bearer_request("/api/x", token), _ok)
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_api_jwt_gate_refuses_an_expired_token():
    from api.jwt_middleware import JWTAuthMiddleware

    secret = os.environ["JWT_SECRET_KEY"]
    mw = JWTAuthMiddleware(app=None, jwt_secret=secret)
    expired = _mint_api_token(secret, jti=uuid.uuid4().hex, ttl_seconds=-3600)
    resp = await mw.dispatch(_bearer_request("/api/x", expired), _ok)
    assert resp.status_code == 401
