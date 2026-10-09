import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.app import fallback_auth_middleware
from core.security.session_tokens import SESSION_AUDIENCE


@pytest.fixture
def client(monkeypatch):
    secret = 'cookie-origin-test-secret-with-sufficient-length'
    monkeypatch.setenv('API_AUTH_TOKEN', 'test-service-token')
    monkeypatch.setenv('JWT_SECRET_KEY', secret)
    monkeypatch.setattr('core.token_denylist.jti_is_revoked', lambda _: False)
    app = FastAPI()
    app.middleware('http')(fallback_auth_middleware)
    @app.post('/api/probe')
    async def probe():
        return {'ok': True}
    token = jwt.encode({'aud': SESSION_AUDIENCE, 'exp': time.time() + 60,
                        'jti': 'cookie-origin', 'user_id': 'tenant'}, secret)
    with TestClient(app, base_url='https://polyrob.test') as test_client:
        test_client.cookies.set('auth_token', token)
        yield test_client, token


@pytest.mark.parametrize('origin', [None, 'https://foreign.test', 'null',
                                  'http://polyrob.test:443', 'https://polyrob.test:invalid',
                                  'https://@polyrob.test', 'https://polyrob.test:0'])
def test_cookie_mutation_refuses_missing_foreign_or_malformed_origin(client, origin):
    headers = {'Origin': origin} if origin else {}
    assert client[0].post('/api/probe', headers=headers).status_code == 403


@pytest.mark.parametrize('headers', [{'Origin': 'https://polyrob.test'},
                                   {'Referer': 'https://polyrob.test/page'},
                                   {'Origin': 'https://polyrob.test:443'}])
def test_cookie_mutation_accepts_same_origin(client, headers):
    assert client[0].post('/api/probe', headers=headers).status_code == 200


def test_explicit_bearer_does_not_require_cookie_origin(client):
    assert client[0].post('/api/probe', headers={'Authorization': f'Bearer {client[1]}'}).status_code == 200
