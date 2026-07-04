"""Tests for the ``/mcp`` slash-command handler (cli/ui/commands/h_mcp.py).

Hermetic: the live manager / static config loader are monkeypatched so no real
MCP servers, config files, or network are touched. The handler is async, so we
drive it with ``asyncio.run`` (mirroring the async tests in test_commands.py).
"""

from __future__ import annotations

import asyncio
import io

from cli.ui.commands import h_mcp as h_mcp_mod
from cli.ui.commands.h_mcp import h_mcp
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


def _plain_ctx(**overrides):
    """Build a CommandContext with a PlainRenderer writing to a StringIO."""
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


class _FakeManager:
    """Stand-in for MCPServerManager.list_servers() (async)."""

    def __init__(self, servers):
        self._servers = servers

    async def list_servers(self):
        return self._servers


class _FakeContainer:
    def __init__(self, service):
        self._service = service

    def get_service(self, name):
        return self._service if name == "mcp" else None


class _FakeMCPTool:
    def __init__(self, manager):
        self.server_manager = manager


# ---------------------------------------------------------------------------
# Live manager path
# ---------------------------------------------------------------------------


def test_live_manager_lists_servers():
    manager = _FakeManager([
        {"name": "anysite", "status": "connected", "enabled": True, "tools_count": 5},
    ])
    ctx, buf = _plain_ctx(container=_FakeContainer(_FakeMCPTool(manager)))
    asyncio.run(h_mcp(ctx))
    out = buf.getvalue()
    assert "anysite" in out
    assert "connected" in out
    assert "5 tools" in out


def test_live_manager_empty_is_graceful():
    ctx, buf = _plain_ctx(container=_FakeContainer(_FakeMCPTool(_FakeManager([]))))
    asyncio.run(h_mcp(ctx))
    assert "No MCP servers configured." in buf.getvalue()


def test_live_manager_error_falls_back_to_config(monkeypatch):
    class _BoomManager:
        async def list_servers(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        h_mcp_mod,
        "_load_static_config",
        lambda: (True, {"filesystem": {"enabled": True, "type": "stdio"}}),
    )
    ctx, buf = _plain_ctx(container=_FakeContainer(_FakeMCPTool(_BoomManager())))
    asyncio.run(h_mcp(ctx))
    out = buf.getvalue()
    assert "filesystem" in out
    assert "from config" in out.lower()


# ---------------------------------------------------------------------------
# Static-config fallback path (no live manager)
# ---------------------------------------------------------------------------


def test_config_fallback_lists_servers(monkeypatch):
    monkeypatch.setattr(h_mcp_mod, "_resolve_manager", lambda ctx: None)
    monkeypatch.setattr(
        h_mcp_mod,
        "_load_static_config",
        lambda: (True, {"anysite": {"enabled": True, "type": "streamable_http"}}),
    )
    ctx, buf = _plain_ctx()
    asyncio.run(h_mcp(ctx))
    out = buf.getvalue()
    assert "anysite" in out
    assert "streamable_http" in out
    assert "enabled" in out


def test_config_disabled_is_graceful(monkeypatch):
    monkeypatch.setattr(h_mcp_mod, "_resolve_manager", lambda ctx: None)
    monkeypatch.setattr(h_mcp_mod, "_load_static_config", lambda: (False, {}))
    ctx, buf = _plain_ctx()
    asyncio.run(h_mcp(ctx))
    assert "MCP disabled." in buf.getvalue()


def test_config_enabled_but_empty_is_graceful(monkeypatch):
    monkeypatch.setattr(h_mcp_mod, "_resolve_manager", lambda ctx: None)
    monkeypatch.setattr(h_mcp_mod, "_load_static_config", lambda: (True, {}))
    ctx, buf = _plain_ctx()
    asyncio.run(h_mcp(ctx))
    assert "No MCP servers configured." in buf.getvalue()


def test_config_disabled_with_servers_shows_them(monkeypatch):
    monkeypatch.setattr(h_mcp_mod, "_resolve_manager", lambda ctx: None)
    monkeypatch.setattr(
        h_mcp_mod,
        "_load_static_config",
        lambda: (False, {"srv": {"enabled": False, "type": "http"}}),
    )
    ctx, buf = _plain_ctx()
    asyncio.run(h_mcp(ctx))
    out = buf.getvalue()
    assert "srv" in out
    assert "disabled" in out.lower()


# ---------------------------------------------------------------------------
# Subcommand handling + no-container safety
# ---------------------------------------------------------------------------


def test_unknown_subcommand_shows_usage():
    ctx, buf = _plain_ctx()
    ctx.args = ["bogus"]
    asyncio.run(h_mcp(ctx))
    assert "Usage" in buf.getvalue()


def test_no_container_falls_back_to_config(monkeypatch):
    # No container at all -> resolver returns None -> static config path.
    monkeypatch.setattr(h_mcp_mod, "_load_static_config", lambda: (False, {}))
    ctx, buf = _plain_ctx()
    asyncio.run(h_mcp(ctx))
    assert "MCP disabled." in buf.getvalue()
