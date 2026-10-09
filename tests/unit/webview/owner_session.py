"""Explicit owner credentials for tests of authenticated console behavior."""
import time
import uuid

import jwt

from core.security.session_tokens import SESSION_AUDIENCE


def owner_headers(monkeypatch):
    from webview import webgate
    secret = 'console-test-owner-secret-' * 3
    monkeypatch.setenv('JWT_SECRET_KEY', secret)
    owner = webgate.local_owner_id()
    token = jwt.encode({'sub': owner, 'user_id': owner, 'role': 'owner', 'tier': 'admin',
                        'aud': SESSION_AUDIENCE, 'jti': uuid.uuid4().hex,
                        'exp': time.time() + 600}, secret, algorithm='HS256')
    return {'Authorization': 'Bearer ' + token, 'Origin': 'http://testserver'}


def authenticate_render_app(app, user_id='u1'):
    """Model authenticated request state in a bare rendering-only router test."""
    @app.middleware('http')
    async def authenticated(request, call_next):
        from api.auth_state import set_auth_state
        set_auth_state(request.state, user_id=user_id, tier='admin', role='owner',
                       authenticated=True, payment_method=None)
        return await call_next(request)
