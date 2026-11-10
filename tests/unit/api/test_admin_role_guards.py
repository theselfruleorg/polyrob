"""The role and tier routes keep the hierarchy block_user keeps, and require_admin
re-reads a database-granted admin role (a demotion holds at once, not after the
token expires). An unreadable user store refuses with 503, never a traceback."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api import admin_endpoints as admin


@pytest.fixture
def db(monkeypatch):
    from core.container import DependencyContainer
    store = SimpleNamespace(fetch_one=AsyncMock(), execute=AsyncMock())
    container = SimpleNamespace(get_service={"database_manager": store}.get)
    monkeypatch.setattr(DependencyContainer, "get_instance", lambda: container)
    monkeypatch.setattr(admin, "get_audit_logger", AsyncMock(return_value=None))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")
    monkeypatch.delenv("ADMIN_WALLETS", raising=False)
    return store


def _request(actor):
    return SimpleNamespace(state=SimpleNamespace(user_id=actor, role=actor,
                                                 wallet_address=None),
                           headers={}, client=None)


_CASES = [
    ("admin", "peer", "admin", False),   # an admin cannot change a peer admin
    ("owner", "peer", "admin", True),    # the owner can
    ("admin", "tenant", "user", True),
    ("admin", "admin", "user", False),   # nobody changes their own account
    ("owner", "owner", "user", False),
    ("owner", "peer", "owner", False),   # the owner row is never changed here
    ("admin", "owner", "user", False),   # the configured owner id is never changed
]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,target,target_role,allowed", _CASES)
async def test_role_route_keeps_the_hierarchy(db, actor, target, target_role, allowed):
    db.fetch_one.return_value = {"role": target_role, "tier": "free", "wallet_address": None}
    call = admin.set_user_role(_request(actor), target, admin.SetRoleRequest(role="user"))
    if allowed:
        assert (await call)["success"]
        db.execute.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403
        db.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,target,target_role,allowed", _CASES)
async def test_tier_route_keeps_the_hierarchy(db, actor, target, target_role, allowed):
    db.fetch_one.return_value = {"role": target_role, "tier": "free", "wallet_address": None}
    call = admin.set_user_tier(_request(actor), target, admin.SetTierRequest(tier="free_access"))
    if allowed:
        assert (await call)["success"]
        db.execute.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403
        db.execute.assert_not_awaited()


# ── require_admin: a database-granted admin role is re-read ─────────────────

@pytest.mark.asyncio
async def test_a_demoted_admin_with_a_live_token_is_refused(db):
    db.fetch_one.return_value = {"role": "user"}
    with pytest.raises(HTTPException) as exc:
        await admin.require_admin(_request("admin"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_current_admin_passes(db):
    db.fetch_one.return_value = {"role": "admin"}
    assert await admin.require_admin(_request("admin")) is True


@pytest.mark.asyncio
async def test_an_admin_with_no_profile_row_is_refused(db):
    db.fetch_one.return_value = None
    with pytest.raises(HTTPException) as exc:
        await admin.require_admin(_request("admin"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_an_unreadable_user_store_refuses_with_503(db):
    db.fetch_one.side_effect = OSError("disk I/O error")
    with pytest.raises(HTTPException) as exc:
        await admin.require_admin(_request("admin"))
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_owner_login_and_admin_wallet_paths_are_unchanged(db, monkeypatch):
    db.fetch_one.side_effect = AssertionError("must not read the store")
    assert await admin.require_admin(_request("owner")) is True
    monkeypatch.setenv("ADMIN_WALLETS", "0xadminwallet")
    req = _request("user")
    req.state.wallet_address = "0xADMINWALLET"
    assert await admin.require_admin(req) is True
