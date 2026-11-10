from core.security.session_tokens import SESSION_AUDIENCE
import time
from types import SimpleNamespace

import jwt
import pytest
from starlette.requests import Request
from starlette.responses import Response


@pytest.fixture(autouse=True)
def _readable_empty_block_list(monkeypatch):
    """Multitenant auth reads the account block list; a readable, empty one admits."""
    from types import SimpleNamespace as _NS
    from unittest.mock import AsyncMock
    store = _NS(fetch_one=AsyncMock(return_value=None))
    monkeypatch.setattr("api.dependencies.require_service", lambda *a, **kw: store)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/session/private", "/api/session/private/feed/events", "/api/session/private/workspace/file/secret.txt"])
@pytest.mark.parametrize("caller,expected", [(None, 401), ("other", 403), ("owner", 200)])
async def test_session_urls_require_owner(monkeypatch, tmp_path, path, caller, expected):
    import webview.server as server
    monkeypatch.setattr(server.webgate, "requires_owner_login", lambda: True)
    monkeypatch.setattr(server.webgate, "posture", lambda: "multitenant")
    monkeypatch.setenv("WEBVIEW_AUTH_ENABLED", "false")  # cannot disable public auth
    secret = "session-token-security-test-secret-32"
    monkeypatch.setenv("JWT_SECRET_KEY", secret)
    monkeypatch.setenv("TOKEN_DENYLIST_PATH", str(tmp_path / "tokens.db"))
    monkeypatch.setattr(server, "pm", lambda: SimpleNamespace(
        clean_session_id=lambda sid: sid, get_session_user=lambda sid: "owner"))
    headers = []
    if caller:
        token = jwt.encode({"aud": SESSION_AUDIENCE, **{"user_id": caller, "exp": time.time() + 60, "jti": "session"}}, secret)
        headers = [(b"authorization", f"Bearer {token}".encode())]
    request = Request({"type": "http", "path": path, "headers": headers, "query_string": b""})

    async def next_handler(request):
        return Response("private data")

    response = await server.auth_middleware(request, next_handler)
    if caller is None and path.startswith("/session/"):
        assert response.status_code in (302, 303)
    else:
        assert response.status_code == expected
    if expected != 200:
        assert b"private data" not in response.body
