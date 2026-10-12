import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.api_key_auth import APIKeyAuthMiddleware, validate_api_key, validate_api_key_cached
from core.container import DependencyContainer
from modules.auth.api_key_manager import APIKeyManager
from modules.database.connection import DatabaseConnection


@pytest_asyncio.fixture
async def keys(tmp_path, monkeypatch):
    conn = DatabaseConnection(tmp_path / 'keys.db')
    await conn.connect()
    await conn.execute('CREATE TABLE api_keys (user_id TEXT, key_hash TEXT UNIQUE, key_prefix TEXT, '
                       'name TEXT, created_at TEXT, expires_at TEXT, is_active INTEGER, '
                       'scopes TEXT, last_used TEXT, revoked_at TEXT)')
    manager = APIKeyManager(conn, SimpleNamespace(get_user_tier=AsyncMock(return_value='holder')))
    container = SimpleNamespace(get_service=lambda name: conn if name == 'database_manager' else None)
    monkeypatch.setattr(DependencyContainer, 'get_instance', classmethod(lambda cls: container))
    yield manager
    await conn.close()


@pytest.mark.asyncio
async def test_issued_key_has_bounded_expiry_and_round_trips_scopes(keys):
    key = await keys.generate_api_key('payer', scopes=['read'])
    info = await validate_api_key(key['api_key'])
    assert info['permissions'] == ['read']
    assert 89 * 86400 < info['api_key_expires_at'] - time.time() <= 90 * 86400
    assert await keys.validate_api_key(key['api_key']) == 'payer'
    listed = await keys.list_user_keys('payer')
    assert listed[0]['prefix'] == key['prefix'] and listed[0]['scopes'] == ['read']


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, 0, -1, True, 1.5, 366, 1000000000])
async def test_invalid_expiry_never_writes_a_key(keys, bad):
    with pytest.raises(ValueError, match='expiry'):
        await keys.generate_api_key('payer', expires_days=bad)
    assert not await keys.list_user_keys('payer')


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, [], ['admin'], ['read', 'read'], ['write', '*'], [True]])
async def test_invalid_scope_never_writes_a_key(keys, bad):
    with pytest.raises(ValueError):
        await keys.generate_api_key('payer', scopes=bad)
    assert not await keys.list_user_keys('payer')


@pytest.mark.asyncio
@pytest.mark.parametrize('column,value', [('scopes', None), ('scopes', '*'),
                                        ('expires_at', None), ('expires_at', 'not-a-date'),
                                        ('expires_at', '2000-01-01T23:59:59+00:00')])
async def test_invalid_legacy_or_expired_keys_cannot_authenticate(keys, column, value):
    key = await keys.generate_api_key('payer')
    await keys.db.execute(f'UPDATE api_keys SET {column}=?', (value,))
    assert await validate_api_key(key['api_key']) is None
    assert await keys.validate_api_key(key['api_key']) is None


@pytest.mark.asyncio
async def test_cache_never_extends_the_key_expiry(keys, monkeypatch):
    from api import api_key_auth
    key = await keys.generate_api_key('payer')
    now = time.time()
    from datetime import datetime, timezone
    expires = datetime.fromtimestamp(now + 2, timezone.utc).isoformat()
    await keys.db.execute('UPDATE api_keys SET expires_at=?', (expires,))
    clock = [now]
    monkeypatch.setattr(api_key_auth.time, 'time', lambda: clock[0])
    cache = {}
    assert await validate_api_key_cached(cache, key['api_key'])
    # Monotonic TTL has not elapsed; wall-clock expiry has. Past +2 by a margin:
    # the ISO round-trip rounds to the microsecond, so +2 exactly can sit a hair
    # BEFORE the stored expiry (a flake).
    clock[0] += 2.5
    assert await validate_api_key_cached(cache, key['api_key']) is None
    assert not cache


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'PROPFIND'])
@pytest.mark.parametrize('header', ['X-API-KEY', 'Authorization'])
def test_read_key_cannot_mutate_with_either_header(monkeypatch, method, header):
    from api import api_key_auth
    info = {'user_id': 'payer', 'permissions': ['read'], 'api_key_expires_at': time.time() + 60}
    monkeypatch.setattr(api_key_auth, 'validate_api_key', AsyncMock(return_value=info))
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware)
    calls = []
    @app.api_route('/probe', methods=['GET', method])
    def probe(request: Request):
        calls.append(request.method)
        return {'authenticated': request.state.authenticated}
    client = TestClient(app)
    key = 'rob_' + 'x' * 40
    headers = {header: 'Bearer ' + key if header == 'Authorization' else key}
    assert client.get('/probe', headers=headers).status_code == 200
    assert client.request(method, '/probe', headers=headers).status_code == 403
    assert calls == ['GET']


@pytest.mark.asyncio
async def test_second_authenticator_cannot_widen_key_scopes(keys):
    from api.middleware import AuthenticationMiddleware
    key = await keys.generate_api_key('payer', scopes=['read'])
    middleware = AuthenticationMiddleware(app=None, secret_key='x' * 40)
    info = await middleware._validate_api_key(key['api_key'])
    request = Request({'type': 'http', 'method': 'PATCH', 'path': '/api/task', 'headers': []})
    assert info['permissions'] == ['read']
    assert not await middleware._check_permissions(request, info)
