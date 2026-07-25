"""T2.4 Task 2 — oauth_bridge: flag-gated header injection + fault isolation.

Uses an injected in-memory OAuthManager (temp Fernet key, dict store) wired in
place of the module singleton, mirroring
tests/unit/tools/oauth/test_oauth_manager.py's fixture style — no disk I/O, no
live endpoints.
"""
import logging
import time

import pytest

from core.exceptions import ConfigurationError
from tools.mcp import oauth_bridge
from tools.mcp.config import MCPServerConfig, MCPServerType
from tools.mcp.security import MCPEncryption
from tools.oauth import OAuthManager, OAuthProvider, OAuthToken

AUTH_BLOCK = {
    "provider": "generic_oauth2",
    "client_id": "cid",
    "auth_url": "https://idp.example/authorize",
    "token_url": "https://idp.example/token",
}


def _http_config(auth=None, headers=None):
    return MCPServerConfig(
        type=MCPServerType.HTTP, url="https://server.example",
        auth=auth, headers=headers or {},
    )


def _stdio_config(auth=None):
    return MCPServerConfig(
        type=MCPServerType.STDIO, command=["python3", "server.py"], auth=auth,
    )


@pytest.fixture()
def fake_manager(monkeypatch):
    """In-memory OAuthManager wired in place of the module singleton."""
    mgr = OAuthManager(encryption=MCPEncryption(key=MCPEncryption.generate_key()), store={})
    monkeypatch.setattr(oauth_bridge, "_get_manager", lambda: mgr)
    return mgr


@pytest.fixture(autouse=True)
def _reset_stdio_warned():
    oauth_bridge._STDIO_WARNED.clear()
    yield
    oauth_bridge._STDIO_WARNED.clear()


# --- flag: default OFF, canonical falsey-set parser -----------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    assert oauth_bridge.mcp_oauth_enabled() is False


def test_flag_explicit_true(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    assert oauth_bridge.mcp_oauth_enabled() is True


# --- truth table: apply_oauth_headers -------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_headers_unchanged_even_with_auth_block(monkeypatch, fake_manager):
    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    cfg = _http_config(auth=AUTH_BLOCK, headers={"X-Foo": "bar"})
    headers = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc")
    assert headers == {"X-Foo": "bar"}
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_flag_on_no_auth_block_headers_unchanged(monkeypatch, fake_manager):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    cfg = _http_config(auth=None, headers={"X-Foo": "bar"})
    headers = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc")
    assert headers == {"X-Foo": "bar"}


@pytest.mark.asyncio
async def test_apply_oauth_headers_returns_a_copy_not_the_same_object(monkeypatch, fake_manager):
    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    original = {"X-Foo": "bar"}
    cfg = _http_config(auth=None, headers=original)
    headers = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc")
    assert headers == original
    assert headers is not original


@pytest.mark.asyncio
async def test_flag_on_auth_and_stored_token_merges_authorization(monkeypatch, fake_manager):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    fake_manager.store_token(
        "u1", "mcp:svc",
        OAuthToken(access_token="AT1", refresh_token="r0", expires_at=time.time() + 3600),
    )
    cfg = _http_config(auth=AUTH_BLOCK, headers={"X-Foo": "bar"})
    headers = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc")
    assert headers["Authorization"] == "Bearer AT1"
    assert headers["X-Foo"] == "bar"  # existing headers preserved


@pytest.mark.asyncio
async def test_no_token_raises_configuration_error(monkeypatch, fake_manager):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    cfg = _http_config(auth=AUTH_BLOCK)
    with pytest.raises(ConfigurationError):
        await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc")


@pytest.mark.asyncio
async def test_malformed_auth_block_raises_configuration_error(monkeypatch, fake_manager):
    """Missing a required GenericOAuth2Provider key (KeyError) is ALSO a config
    fault — dropping only this server, never a raw KeyError escaping."""
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    cfg = _http_config(auth={"provider": "generic_oauth2"})  # missing client_id/auth_url/token_url
    with pytest.raises(ConfigurationError):
        await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc")


@pytest.mark.asyncio
async def test_stdio_with_auth_ignored_and_logged_once(monkeypatch, fake_manager, caplog):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    cfg = _stdio_config(auth=AUTH_BLOCK)
    with caplog.at_level(logging.WARNING, logger="tools.mcp.oauth_bridge"):
        headers1 = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc-stdio")
        headers2 = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc-stdio")
    assert headers1 == {}
    assert headers2 == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # logged ONCE per server name, not once per call
    assert "stdio" in warnings[0].message.lower()


@pytest.mark.asyncio
async def test_stdio_without_auth_no_warning(monkeypatch, fake_manager, caplog):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    cfg = _stdio_config(auth=None)
    with caplog.at_level(logging.WARNING, logger="tools.mcp.oauth_bridge"):
        headers = await oauth_bridge.apply_oauth_headers(cfg, "u1", "svc-stdio2")
    assert headers == {}
    assert caplog.records == []


# --- oauth_applies_to: the shared predicate --------------------------------------

def test_oauth_applies_to_true_only_when_flag_auth_and_non_stdio(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    assert oauth_bridge.oauth_applies_to(_http_config(auth=AUTH_BLOCK)) is True
    assert oauth_bridge.oauth_applies_to(_http_config(auth=None)) is False
    assert oauth_bridge.oauth_applies_to(_stdio_config(auth=AUTH_BLOCK)) is False


def test_oauth_applies_to_false_when_flag_off(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    assert oauth_bridge.oauth_applies_to(_http_config(auth=AUTH_BLOCK)) is False


# --- make_auth_refresh_callback: expire + re-mint --------------------------------

class _RefreshingProvider(OAuthProvider):
    name = "mcp:svc"

    def __init__(self):
        self.refresh_calls = 0

    def authorize_url(self, *, state=None, redirect_uri=None):
        return "https://idp.example/authorize"

    async def exchange_code(self, code, *, redirect_uri=None):
        raise NotImplementedError

    async def refresh(self, token):
        self.refresh_calls += 1
        return OAuthToken(
            access_token=f"refreshed-{self.refresh_calls}",
            refresh_token=token.refresh_token,
            expires_at=time.time() + 3600,
        )


@pytest.mark.asyncio
async def test_refresh_callback_expires_stale_token_and_remints(monkeypatch, fake_manager):
    prov = _RefreshingProvider()
    fake_manager.register(prov)
    fake_manager.store_token(
        "u1", "mcp:svc",
        OAuthToken(access_token="old", refresh_token="r0", expires_at=time.time() + 3600),
    )

    cb = oauth_bridge.make_auth_refresh_callback("u1", "svc")
    new_auth = await cb()

    assert new_auth == "Bearer refreshed-1"
    assert prov.refresh_calls == 1
    # the manager's stored token was updated to the refreshed one
    assert fake_manager.load_token("u1", "mcp:svc").access_token == "refreshed-1"


@pytest.mark.asyncio
async def test_refresh_callback_raises_when_no_refresh_token(monkeypatch, fake_manager):
    prov = _RefreshingProvider()
    fake_manager.register(prov)
    fake_manager.store_token(
        "u1", "mcp:svc",
        OAuthToken(access_token="old", refresh_token=None, expires_at=time.time() + 3600),
    )
    cb = oauth_bridge.make_auth_refresh_callback("u1", "svc")
    with pytest.raises(Exception):
        await cb()
