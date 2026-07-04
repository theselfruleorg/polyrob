"""H1 review-followup: role="owner" must be mintable ONLY by the
password-gated owner-login (webview/owner_auth.py::issue_owner_session_cookie).

H1 (commit ac154ab0) added "owner" to VALID_ROLES/ADMIN_ROLES so owner-login
sessions are recognized as admin-by-role. Side effect: the admin-gated
POST /admin/users/{user_id}/role endpoint (api/admin_endpoints.py::
set_user_role) validated the requested role against VALID_ROLES, which now
included "owner" — so an admin caller could assign role="owner" to an
arbitrary user, letting them mint owners without ever touching the
password-gated owner-login flow.

Fix: set_user_role now validates against ASSIGNABLE_ROLES (VALID_ROLES minus
"owner") via core.constants.validate_assignable_role. "owner" stays in
VALID_ROLES/ADMIN_ROLES for read-side admin checks (require_admin, is_admin).
"""
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.constants import ASSIGNABLE_ROLES, VALID_ROLES, validate_assignable_role


def _state(**kwargs):
    return types.SimpleNamespace(**kwargs)


class _FakeRequest:
    def __init__(self, **state_kwargs):
        self.state = _state(**state_kwargs)
        self.client = None
        self.headers = {}


class _FakeDB:
    def __init__(self, current_role="user"):
        self._current_role = current_role
        self.executed = []

    async def fetch_one(self, query, params):
        return {"role": self._current_role}

    async def execute(self, query, params):
        self.executed.append((query, params))
        return None


class _FakeContainer:
    def __init__(self, db):
        self._db = db
        self.config = None

    def get_service(self, name):
        return {"database_manager": self._db}.get(name)


# --- core/constants.py: ASSIGNABLE_ROLES excludes "owner" -------------------

def test_owner_not_in_assignable_roles():
    assert "owner" not in ASSIGNABLE_ROLES


def test_assignable_roles_is_valid_roles_minus_owner():
    assert ASSIGNABLE_ROLES == set(VALID_ROLES) - {"owner"}


def test_validate_assignable_role_rejects_owner():
    with pytest.raises(ValueError):
        validate_assignable_role("owner")


def test_validate_assignable_role_accepts_user_and_admin():
    assert validate_assignable_role("user") is True
    assert validate_assignable_role("admin") is True


# --- api/admin_endpoints.py::set_user_role — assignment boundary -----------

@pytest.mark.asyncio
async def test_set_user_role_rejects_owner_even_for_admin_caller(monkeypatch):
    """An admin caller POSTing role="owner" must get a 400, not a silent
    grant. This is the core regression this task closes."""
    from api.admin_endpoints import set_user_role, SetRoleRequest
    from core.container import DependencyContainer

    db = _FakeDB(current_role="user")
    monkeypatch.setattr(DependencyContainer, "get_instance", staticmethod(lambda: _FakeContainer(db)))

    request = _FakeRequest(role="admin", user_id="admin-caller", wallet_address=None)

    with pytest.raises(HTTPException) as exc_info:
        await set_user_role(request, "victim-user", SetRoleRequest(role="owner"))

    assert exc_info.value.status_code == 400
    # Must fail BEFORE any DB write.
    assert db.executed == []


@pytest.mark.asyncio
async def test_set_user_role_still_allows_normal_roles(monkeypatch):
    """Non-owner assignable roles (user/admin) must still work post-fix."""
    from api.admin_endpoints import set_user_role, SetRoleRequest
    from core.container import DependencyContainer

    db = _FakeDB(current_role="user")
    monkeypatch.setattr(DependencyContainer, "get_instance", staticmethod(lambda: _FakeContainer(db)))

    request = _FakeRequest(role="admin", user_id="admin-caller", wallet_address=None)

    result = await set_user_role(request, "victim-user", SetRoleRequest(role="admin"))

    assert result["success"] is True
    assert result["new_role"] == "admin"
    assert db.executed, "expected a DB update to have run"


@pytest.mark.asyncio
async def test_set_user_role_rejects_owner_via_http(monkeypatch):
    """End-to-end through the router: POST .../role {"role": "owner"} -> 400."""
    from api.admin_endpoints import router, require_admin
    from core.container import DependencyContainer

    db = _FakeDB(current_role="user")
    monkeypatch.setattr(DependencyContainer, "get_instance", staticmethod(lambda: _FakeContainer(db)))

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[require_admin] = lambda: True

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/admin/users/victim-user/role", json={"role": "owner"})

    assert resp.status_code == 400, resp.text
    assert db.executed == []


# --- owner-login path is unchanged: still mints role="owner" ---------------

def test_owner_auth_still_mints_owner_role():
    """webview/owner_auth.py::issue_owner_session_cookie remains the sole
    legitimate source of role="owner" — unchanged by this fix."""
    import inspect
    import webview.owner_auth as owner_auth

    src = inspect.getsource(owner_auth.issue_owner_session_cookie)
    assert '"owner"' in src or "'owner'" in src
