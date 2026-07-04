"""H10 + C5:
- H10 part 1: the @BaseTool.action decorator sat on the PRIVATE _invalidate_resource_cache
  (skipped by get_actions for the leading '_') while subscribe_resource had no decorator,
  so subscribe was dead (agent could unsubscribe but never subscribe).
- C5: neither subscribe_resource nor unsubscribe_resource enforced the _enabled +
  requested_servers allowlist that every other MCP action does, so a session could
  tear down / touch another tenant's server subscription (cross-tenant tamper/DoS).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from core.exceptions import ToolError
from tools.mcp.mcp_tool import MCPTool
from tools.mcp.views import MCPSubscribeResourceAction, MCPUnsubscribeResourceAction


# ── H10 part 1: subscribe is a registered action, the cache callback is not ──

def test_subscribe_resource_is_a_registered_action():
    assert hasattr(MCPTool.subscribe_resource, "_description")
    assert getattr(MCPTool.subscribe_resource, "_param_model") is MCPSubscribeResourceAction


def test_invalidate_cache_callback_is_not_an_action():
    # Private callback must not carry action metadata (it's a resource-update handler).
    assert not hasattr(MCPTool._invalidate_resource_cache, "_description")


# ── C5: allowlist guard on both subscribe and unsubscribe ──

def _tool(requested):
    t = MCPTool.__new__(MCPTool)
    t._enabled = True
    t.requested_servers = requested
    t.ensure_initialized = AsyncMock()
    t.server_manager = AsyncMock()
    return t


def test_subscribe_rejects_server_outside_allowlist():
    t = _tool(requested={"allowed"})
    with pytest.raises(ToolError):
        asyncio.run(t.subscribe_resource(
            MCPSubscribeResourceAction(server_name="forbidden", resource_uri="file:///x")
        ))
    t.server_manager.subscribe_resource.assert_not_called()


def test_unsubscribe_rejects_server_outside_allowlist():
    t = _tool(requested={"allowed"})
    with pytest.raises(ToolError):
        asyncio.run(t.unsubscribe_resource(
            MCPUnsubscribeResourceAction(server_name="forbidden", resource_uri="file:///x")
        ))
    t.server_manager.unsubscribe_resource.assert_not_called()


def test_subscribe_allows_server_in_allowlist():
    t = _tool(requested={"allowed"})
    t.server_manager.subscribe_resource = AsyncMock(return_value={"success": True})
    out = asyncio.run(t.subscribe_resource(
        MCPSubscribeResourceAction(server_name="allowed", resource_uri="file:///x")
    ))
    assert out["success"] is True
    t.server_manager.subscribe_resource.assert_awaited_once()


def test_disabled_service_rejects_subscribe():
    t = _tool(requested=None)
    t._enabled = False
    with pytest.raises(ToolError):
        asyncio.run(t.subscribe_resource(
            MCPSubscribeResourceAction(server_name="any", resource_uri="file:///x")
        ))
