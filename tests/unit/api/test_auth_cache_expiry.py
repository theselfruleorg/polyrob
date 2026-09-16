import time

import jwt
import pytest

from api.middleware import AuthenticationMiddleware


@pytest.mark.asyncio
async def test_cached_jwt_expires(monkeypatch, tmp_path):
    secret = "security-test-secret-at-least-32-bytes"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "tokens.db"))
    expiry = int(time.time()) + 60
    token = jwt.encode({"user_id": "owner", "jti": "session", "exp": expiry}, secret)
    middleware = AuthenticationMiddleware(None, secret_key=secret)
    assert await middleware._validate_auth(f"Bearer {token}", "")
    monkeypatch.setattr("api.middleware.time.time", lambda: expiry + 1)
    assert await middleware._validate_auth(f"Bearer {token}", "") is None


@pytest.mark.asyncio
async def test_nonexpiring_jwt_is_not_cached(monkeypatch, tmp_path):
    secret = "security-test-secret-at-least-32-bytes"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "tokens.db"))
    token = jwt.encode({"user_id": "owner", "jti": "session"}, secret)
    middleware = AuthenticationMiddleware(None, secret_key=secret)
    assert await middleware._validate_auth(f"Bearer {token}", "") is None
    assert token not in middleware.token_cache
