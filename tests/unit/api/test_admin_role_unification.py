"""H1: collapse the two admin-truth sources onto ONE canonical `role`.

Pre-fix state (final coherence review, C4-M2 + N1):
  - api/admin_endpoints.py::require_admin read request.state.is_admin — a
    SECOND admin-truth source separate from the canonical `role` that
    verify_payment_for_request/extract_admin_info use.
  - webview/owner_auth.py mints owner sessions with role="owner" + tier="admin",
    but "owner" was NOT in ADMIN_ROLES/VALID_ROLES (core/constants.py), so the
    owner-login session was admin-by-tier but NOT admin-by-role: is_admin("owner")
    was False, so the owner never got the payment admin-bypass on a billed path.

Fix:
  - "owner" added to ADMIN_ROLES/VALID_ROLES (core/constants.py) — the instance
    operator authenticating to their own console legitimately IS an admin.
  - require_admin now derives admin status from request.state.role (via the
    same is_admin()/extract_admin_info() helper the payment path uses) instead
    of a separately-set request.state.is_admin flag.
  - api/app.py's fallback_auth_middleware also had its OWN hardcoded
    `role == 'admin'` check (a THIRD ad-hoc admin path) instead of using the
    canonical is_admin_role() — fixed so it recognizes any role in ADMIN_ROLES
    (now including "owner"), not just the literal string 'admin'.
"""
import types

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.constants import ADMIN_ROLES, VALID_ROLES, is_admin
from api.admin_endpoints import require_admin


def _state(**kwargs):
    """Minimal request.state-like object (duck-typed, matches extract_admin_info)."""
    return types.SimpleNamespace(**kwargs)


class _FakeRequest:
    def __init__(self, **state_kwargs):
        self.state = _state(**state_kwargs)


# --- core/constants.py: "owner" is a recognized admin role -----------------

def test_owner_role_in_admin_roles():
    assert "owner" in ADMIN_ROLES


def test_owner_role_in_valid_roles():
    assert "owner" in VALID_ROLES


def test_is_admin_true_for_owner_role():
    assert is_admin(role="owner") is True


def test_is_admin_false_for_plain_user_role():
    assert is_admin(role="user") is False


def test_is_admin_false_for_no_role():
    # Default/anonymous — must NOT be granted admin.
    assert is_admin() is False


# --- require_admin reads canonical `role`, not a second is_admin flag ------

@pytest.mark.asyncio
async def test_require_admin_passes_for_owner_role_session():
    """Owner-login session (role='owner') → require_admin passes.
    This was DENIED pre-fix (is_admin('owner') was False)."""
    request = _FakeRequest(role="owner", wallet_address=None)
    assert await require_admin(request) is True


@pytest.mark.asyncio
async def test_require_admin_passes_for_admin_role_session_no_regression():
    """A request authenticated via a path that sets role='admin' must still
    pass require_admin after the fix (no regression for the existing path)."""
    request = _FakeRequest(role="admin", wallet_address=None)
    assert await require_admin(request) is True


@pytest.mark.asyncio
async def test_require_admin_passes_for_admin_wallet_no_role():
    """A request authenticated purely by admin wallet (role not admin) must
    still pass — is_admin() is role-OR-wallet."""
    request = _FakeRequest(role="user", wallet_address="0xADMINWALLET")
    import core.constants as constants

    async def _fake_is_admin_wallet(monkeypatch):
        pass

    # Use monkeypatch-free approach: exercise via ADMIN_WALLETS env directly.
    import os
    old = os.environ.get("ADMIN_WALLETS")
    os.environ["ADMIN_WALLETS"] = "0xadminwallet"
    try:
        assert await require_admin(request) is True
    finally:
        if old is None:
            os.environ.pop("ADMIN_WALLETS", None)
        else:
            os.environ["ADMIN_WALLETS"] = old


@pytest.mark.asyncio
async def test_require_admin_denies_anonymous_or_user_role_no_over_grant():
    """An anonymous/user-role request must still be 403'd — no over-grant."""
    from fastapi import HTTPException

    request = _FakeRequest(role="user", wallet_address=None)
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_denies_when_role_attribute_missing():
    """No role set at all on request.state (unauthenticated request that
    somehow reached the dependency) — extract_admin_info defaults role to
    'user', so this must NOT be granted admin."""
    from fastapi import HTTPException

    request = _FakeRequest()  # no role, no wallet_address
    with pytest.raises(HTTPException):
        await require_admin(request)


# --- app.py fallback_auth_middleware: no THIRD ad-hoc admin check ----------

class _FakeContainer:
    config = None

    def get_service(self, name):
        return None


def _fallback_app():
    from api.app import fallback_auth_middleware

    app = FastAPI()

    @app.get("/api/admin/probe")
    async def probe(request: Request):
        return {"is_admin": getattr(request.state, "is_admin", False), "role": request.state.role}

    app.middleware("http")(fallback_auth_middleware)
    return TestClient(app, raise_server_exceptions=False)


def test_fallback_middleware_grants_admin_for_owner_role_jwt(monkeypatch):
    """A cookie-carried JWT with role='owner' (exactly what
    webview/owner_auth.py mints) must set request.state.is_admin=True via the
    fallback middleware's JWT-decode branch — this branch previously
    hardcoded `role == 'admin'` and would have denied 'owner'."""
    import os
    import jwt as pyjwt
    from core.container import DependencyContainer

    monkeypatch.setenv("API_AUTH_TOKEN", "unused-but-required")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        DependencyContainer, "get_instance",
        classmethod(lambda cls, *a, **k: _FakeContainer()),
    )

    token = pyjwt.encode(
        {"sub": "local-owner", "user_id": "local-owner", "tier": "admin", "role": "owner"},
        "test-secret",
        algorithm="HS256",
    )

    client = _fallback_app()
    resp = client.get("/api/admin/probe", cookies={"auth_token": token})

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "owner"
    assert resp.json()["is_admin"] is True
