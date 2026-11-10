"""A blocked account can neither sign in (/api/auth/verify) nor keep using the
multitenant console with a token it already holds. Both read the ONE block
predicate in api/account_access.py; an unreadable block list refuses (503)."""
import importlib
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

WALLET = "0x0000000000000000000000000000000000000001"


class _FakeSiwe:
    async def verify_signature(self, **kwargs):
        return True


class _FakeIdentityMapper:
    async def get_or_create_user(self, wallet_address, chain):
        return "u-fixture-1"


class _FakeDB:
    def __init__(self, blocked=None, fail=False):
        self.blocked, self.fail = blocked, fail

    async def fetch_one(self, query, params):
        if "blocked_users" in query:
            if self.fail:
                raise OSError("unreadable")
            return None if self.blocked is None else {"is_blocked": self.blocked}
        return {"role": "user", "tier": "free"}


def _container(db):
    services = {"siwe_authenticator": _FakeSiwe(),
                "identity_mapper": _FakeIdentityMapper(),
                "database_manager": db}
    return SimpleNamespace(config=SimpleNamespace(jwt_secret_key="test-secret-key"),
                           get_service=services.get)


async def _verify(monkeypatch, db):
    import api.auth_endpoints as ae
    from core.container import DependencyContainer
    monkeypatch.delenv("ADMIN_WALLETS", raising=False)
    monkeypatch.setattr(DependencyContainer, "get_instance",
                        staticmethod(lambda: _container(db)))
    req = ae.VerifyRequest(wallet_address=WALLET, message="m", signature="s", nonce="n")
    return await ae.verify_signature(req)


@pytest.mark.asyncio
async def test_a_blocked_account_cannot_sign_in(monkeypatch):
    with pytest.raises(HTTPException) as err:
        await _verify(monkeypatch, _FakeDB(blocked=1))
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_an_unreadable_block_list_refuses_sign_in(monkeypatch):
    with pytest.raises(HTTPException) as err:
        await _verify(monkeypatch, _FakeDB(fail=True))
    assert err.value.status_code == 503


@pytest.mark.asyncio
async def test_an_unblocked_account_signs_in(monkeypatch):
    out = await _verify(monkeypatch, _FakeDB(blocked=0))
    assert out is not None


# ── the multitenant console: an existing token of a blocked account ─────────

def _mint(secret):
    import jwt
    from core.security.session_tokens import SESSION_AUDIENCE
    return jwt.encode({"sub": WALLET, "user_id": "u-fixture-1", "role": "user",
                       "tier": "free", "jti": uuid.uuid4().hex,
                       "iat": datetime.utcnow(),
                       "exp": datetime.utcnow() + timedelta(hours=1),
                       "aud": SESSION_AUDIENCE}, secret, algorithm="HS256")


@pytest.fixture
def multitenant_client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_POSTURE", "multitenant")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    import webview.webgate as wg
    importlib.reload(wg)
    import webview.server as srv
    importlib.reload(srv)
    yield TestClient(srv._fastapi, raise_server_exceptions=False)
    monkeypatch.delenv("POLYROB_POSTURE", raising=False)
    importlib.reload(wg)
    importlib.reload(srv)


@pytest.mark.parametrize("blocked,fail,status", [(1, False, 403), (None, True, 503)])
def test_the_console_refuses_a_blocked_or_unreadable_account(
        multitenant_client, monkeypatch, blocked, fail, status):
    db = _FakeDB(blocked=blocked, fail=fail)
    monkeypatch.setattr("api.dependencies.require_service", lambda *a, **kw: db)
    resp = multitenant_client.get(
        "/api/task/capabilities",
        headers={"Authorization": f"Bearer {_mint('test-secret-key')}"})
    assert resp.status_code == status, resp.text


def test_the_console_admits_an_unblocked_account(multitenant_client, monkeypatch):
    db = _FakeDB(blocked=None)
    monkeypatch.setattr("api.dependencies.require_service", lambda *a, **kw: db)
    resp = multitenant_client.get(
        "/api/task/capabilities",
        headers={"Authorization": f"Bearer {_mint('test-secret-key')}"})
    assert resp.status_code not in (401, 403, 503), resp.text
