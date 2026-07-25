"""T2.4 Task 2 — construction-site wiring: flag OFF is byte-identical.

Covers the three seams `tools/mcp/oauth_bridge.py` is wired into:
- `server_manager.py::MCPServerManager._connect_server` (SSE/HTTP/streamable)
- `user_mcp_service.py::UserMCPService._test_mcp_protocol_with_config`
- `user_mcp_service.py::UserMCPService._test_mcp_protocol`

All three assert: with MCP_OAUTH_ENABLED unset/off, the transport receives
headers EQUAL to config.headers (content, not object-identity) and
on_auth_refresh=None — i.e. the bridge has zero observable effect. Transport
classes and MCPClient are patched out (no network); this pins wiring, not
transport internals (already covered by test_transport_auth_retry.py and
existing SSE/HTTP/streamable protocol tests).
"""
import logging

import pytest

from tools.mcp.config import MCPServerConfig, MCPServerType

AUTH_BLOCK = {
    "provider": "generic_oauth2",
    "client_id": "cid",
    "auth_url": "https://idp.example/authorize",
    "token_url": "https://idp.example/token",
}


class _FakeClient:
    """Stands in for MCPClient — no real connect/close, no tools discovered."""

    tools = []

    def __init__(self, transport):
        self.transport = transport

    async def connect(self):
        pass

    async def close(self):
        pass


class _CapturingTransport:
    """Records every kwarg it was constructed with; instances share the class-
    level list so a test can inspect the most recent call."""

    calls = []

    def __init__(self, **kwargs):
        kwargs["_class"] = self.__class__.__name__
        type(self).calls.append(kwargs)


def _reset(transport_cls):
    transport_cls.calls = []


# --- site 1: server_manager.py::_connect_server ---------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("server_type,patch_name", [
    (MCPServerType.SSE, "MCPSSETransport"),
    (MCPServerType.HTTP, "MCPHTTPTransport"),
    (MCPServerType.STREAMABLE_HTTP, "MCPStreamableHTTPTransport"),
])
async def test_connect_server_flag_off_headers_and_callback_byte_identical(
    monkeypatch, server_type, patch_name
):
    from tools.mcp.server_manager import MCPServerManager, ServerConnection

    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    _reset(_CapturingTransport)
    monkeypatch.setattr(f"tools.mcp.server_manager.{patch_name}", _CapturingTransport)
    monkeypatch.setattr("tools.mcp.server_manager.MCPClient", _FakeClient)

    cfg = MCPServerConfig(
        type=server_type,
        url="https://server.example",
        headers={"X-Foo": "bar"},
        auth=AUTH_BLOCK,  # present, but flag is off -> must have zero effect
    )
    conn = ServerConnection(name="global-svc", config=cfg)

    mgr = MCPServerManager()
    ok = await mgr._connect_server(conn)

    assert ok is True
    assert len(_CapturingTransport.calls) == 1
    call = _CapturingTransport.calls[0]
    assert call["headers"] == {"X-Foo": "bar"}
    assert call.get("on_auth_refresh") is None


@pytest.mark.asyncio
async def test_connect_server_stdio_unaffected_by_bridge(monkeypatch):
    """STDIO never touches headers/on_auth_refresh at all — the bridge only
    logs (verified separately in test_oauth_bridge.py); this just pins that
    the stdio transport construction kwargs are untouched."""
    from tools.mcp.server_manager import MCPServerManager, ServerConnection

    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")  # even with flag ON
    _reset(_CapturingTransport)
    monkeypatch.setattr("tools.mcp.server_manager.MCPStdioTransport", _CapturingTransport)
    monkeypatch.setattr("tools.mcp.server_manager.MCPClient", _FakeClient)

    cfg = MCPServerConfig(
        type=MCPServerType.STDIO,
        command=["python3", "server.py"],
        auth=AUTH_BLOCK,
    )
    conn = ServerConnection(name="global-stdio-svc", config=cfg)

    mgr = MCPServerManager()
    ok = await mgr._connect_server(conn)

    assert ok is True
    call = _CapturingTransport.calls[0]
    assert "headers" not in call
    assert "on_auth_refresh" not in call


@pytest.mark.asyncio
async def test_connect_server_user_registered_derives_user_id_from_connection_name(
    monkeypatch,
):
    """user_{user_id}::{server_name} connection names carry a real per-tenant
    user_id through to the bridge (asserted via a token stored under that id
    being picked up once the flag is on)."""
    from tools.mcp.server_manager import MCPServerManager, ServerConnection
    from tools.mcp import oauth_bridge
    from tools.mcp.security import MCPEncryption
    from tools.oauth import OAuthManager, OAuthToken
    import time as _time

    monkeypatch.setenv("MCP_OAUTH_ENABLED", "true")
    mgr_oauth = OAuthManager(encryption=MCPEncryption(key=MCPEncryption.generate_key()), store={})
    mgr_oauth.store_token(
        "alice", "mcp:user_alice::svc",
        OAuthToken(access_token="AT-ALICE", refresh_token="r0", expires_at=_time.time() + 3600),
    )
    monkeypatch.setattr(oauth_bridge, "_get_manager", lambda: mgr_oauth)

    _reset(_CapturingTransport)
    monkeypatch.setattr("tools.mcp.server_manager.MCPHTTPTransport", _CapturingTransport)
    monkeypatch.setattr("tools.mcp.server_manager.MCPClient", _FakeClient)

    cfg = MCPServerConfig(type=MCPServerType.HTTP, url="https://server.example", auth=AUTH_BLOCK)
    conn = ServerConnection(name="user_alice::svc", config=cfg)

    mgr = MCPServerManager()
    ok = await mgr._connect_server(conn)

    assert ok is True
    call = _CapturingTransport.calls[0]
    assert call["headers"]["Authorization"] == "Bearer AT-ALICE"
    assert callable(call["on_auth_refresh"])


# --- site 2: user_mcp_service.py::_test_mcp_protocol_with_config ----------------

@pytest.mark.asyncio
async def test_test_mcp_protocol_with_config_flag_off_byte_identical(monkeypatch, caplog):
    from tools.mcp.user_mcp_service import UserMCPService

    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    _reset(_CapturingTransport)

    svc = UserMCPService.__new__(UserMCPService)
    svc.logger = logging.getLogger("test-user-mcp-service")

    cfg = MCPServerConfig(
        type=MCPServerType.HTTP, url="https://server.example",
        headers={"X-Foo": "bar"}, auth=AUTH_BLOCK,
    )

    # _test_mcp_protocol_with_config imports MCPClient/MCPHTTPTransport locally
    # from tools.mcp.protocol — patch the class objects it will resolve there.
    import tools.mcp.protocol as protocol_mod
    monkeypatch.setattr(protocol_mod, "MCPHTTPTransport", _CapturingTransport)
    monkeypatch.setattr(protocol_mod, "MCPClient", _FakeClient)

    result = await svc._test_mcp_protocol_with_config(cfg, user_id="bob", server_name="svc")

    assert len(_CapturingTransport.calls) == 1
    call = _CapturingTransport.calls[0]
    assert call["headers"] == {"X-Foo": "bar"}
    assert call.get("on_auth_refresh") is None
    assert result.success is True


# --- site 3: user_mcp_service.py::_test_mcp_protocol ----------------------------

@pytest.mark.asyncio
async def test_test_mcp_protocol_flag_off_byte_identical(monkeypatch):
    from tools.mcp.user_mcp_service import UserMCPService
    from unittest.mock import AsyncMock
    import tools.mcp.protocol as protocol_mod

    monkeypatch.delenv("MCP_OAUTH_ENABLED", raising=False)
    _reset(_CapturingTransport)
    monkeypatch.setattr(protocol_mod, "MCPHTTPTransport", _CapturingTransport)
    monkeypatch.setattr(protocol_mod, "MCPClient", _FakeClient)

    svc = UserMCPService.__new__(UserMCPService)
    svc.logger = logging.getLogger("test-user-mcp-service")
    svc.db = AsyncMock()

    cfg = MCPServerConfig(
        type=MCPServerType.HTTP, url="https://server.example",
        headers={"X-Foo": "bar"}, auth=AUTH_BLOCK,
    )

    await svc._test_mcp_protocol("bob", "svc", cfg, start_time=0.0)

    assert len(_CapturingTransport.calls) == 1
    call = _CapturingTransport.calls[0]
    assert call["headers"] == {"X-Foo": "bar"}
    assert call.get("on_auth_refresh") is None
