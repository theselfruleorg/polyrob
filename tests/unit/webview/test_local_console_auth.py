"""A loopback source address is not an owner credential."""
import importlib
import re
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from core.security.session_tokens import SESSION_AUDIENCE


@pytest.fixture
def local_console(monkeypatch, tmp_path):
    from argon2 import PasswordHasher
    monkeypatch.setenv('POLYROB_POSTURE', 'local')
    monkeypatch.setenv('POLYROB_OWNER_USER_ID', 'owner')
    monkeypatch.setenv('POLYROB_OWNER_USERNAME', 'operator')
    monkeypatch.setenv('POLYROB_OWNER_PASSWORD_HASH', PasswordHasher().hash('password'))
    monkeypatch.setenv('JWT_SECRET_KEY', 'local-test-secret-' * 3)
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEBVIEW_AUTH_ENABLED', 'false')
    import webview.server as server
    importlib.reload(server)
    client = TestClient(server.app, base_url='http://127.0.0.1')
    yield client, server
    importlib.reload(server)


def test_loopback_cannot_read_or_mutate_console_without_login(local_console):
    client, _ = local_console
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 302
    assert root.headers["location"].startswith("/owner-login")
    assert client.get('/api/webgate/flags').status_code == 401
    response = client.post('/api/task/sessions', json={}, headers={'Origin': 'http://127.0.0.1'})
    assert response.status_code == 401


def test_local_owner_password_round_trip(local_console):
    client, _ = local_console
    page = client.get('/owner-login')
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)
    response = client.post('/owner-login', data={
        'username': 'operator', 'password': 'password', 'csrf_token': csrf,
    }, headers={'Origin': 'http://127.0.0.1'}, follow_redirects=False)
    assert response.status_code == 303
    assert 'auth_token' in response.cookies
    assert 'Secure' not in response.headers['set-cookie']  # local HTTP is deliberate
    assert client.get('/api/webgate/flags').status_code == 200
    client.post('/logout', headers={'Origin': 'http://127.0.0.1'})
    assert client.get('/api/webgate/flags').status_code == 401


@pytest.mark.asyncio
async def test_local_socket_refuses_anonymous_and_wrong_owner(local_console, monkeypatch):
    _, server = local_console
    assert await server.connect('anonymous', {'REMOTE_ADDR': '127.0.0.1'}) is False
    token = jwt.encode({'user_id': 'other', 'sub': 'other', 'role': 'admin',
                        'exp': time.time() + 60, 'jti': 'local-wrong-owner',
                        'aud': SESSION_AUDIENCE}, 'local-test-secret-' * 3, algorithm='HS256')
    assert await server.connect('wrong-owner', {'REMOTE_ADDR': '127.0.0.1'}, {'token': token}) is False
    assert 'anonymous' not in server._socket_user
    assert 'wrong-owner' not in server._socket_user


def test_local_session_access_requires_owner_identity(local_console):
    from webview.session_access import owner_may_open
    assert not owner_may_open(None, 'owner')
    assert not owner_may_open('other', 'other')
    assert owner_may_open('owner', 'room-tenant')


def test_anonymous_theme_does_not_read_owner_preferences(local_console, monkeypatch):
    from starlette.requests import Request
    from webview.theme import theme_preference, show_avatar
    def forbidden(*args, **kwargs):
        pytest.fail("anonymous request read owner preferences")
    monkeypatch.setattr("core.prefs.resolve", forbidden)
    request = Request({"type": "http", "headers": []})
    assert theme_preference(request) == "auto"
    assert show_avatar(request) is True
