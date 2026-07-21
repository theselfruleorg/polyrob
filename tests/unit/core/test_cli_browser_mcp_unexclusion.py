"""S3 (dynamic tool rig / browser un-exclusion inbox item, 2026-07-19): the headless
CLI container serves `browser` (shipped by the maint loop @14381f62 — eager
BrowserManager wrapper init + `browser` alias; Chromium itself stays lazy in
get_playwright_browser) AND `mcp` (this change — email-2026-07-14 precedent:
MCPTool.__init__ is config parsing only; server connections live in the DEFERRED
async initialize()). S1's catalog then reports them `loadable`/`loaded` instead of
`unavailable-on-this-deploy`, and sessions on `polyrob telegram` actually get the
tools the autonomous grant hands out.
"""
import asyncio
import importlib.util
import types

import pytest

from core.bootstrap import (
    _CLI_INCOMPATIBLE,
    _CLI_REGISTERABLE_TOOLS,
    cli_unavailable_tools,
    register_cli_tools,
)

_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


class _FakeContainer:
    def __init__(self):
        self._svc = {}
        self.config = types.SimpleNamespace()

    def has_service(self, name):
        return name in self._svc

    def register_service(self, name, obj):
        self._svc[name] = obj

    def register_required_service(self, name, obj):
        self._svc[name] = obj

    def get_service(self, name):
        return self._svc.get(name)


def test_browser_and_mcp_no_longer_hard_excluded():
    assert "browser_manager" not in _CLI_INCOMPATIBLE
    assert "browser" not in _CLI_INCOMPATIBLE
    assert "mcp" not in _CLI_INCOMPATIBLE


@pytest.mark.skipif(not _PLAYWRIGHT, reason="playwright not installed")
def test_browser_manager_registers_with_alias():
    """Shipped behavior (@14381f62): the dedicated pre-loop block registers the
    manager AND the `browser` alias service (the Browser wrapper object; the
    Chromium launch stays lazy inside get_playwright_browser)."""
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))
    assert c.has_service("browser_manager")
    assert c.has_service("browser")


def test_mcp_registers_when_enabled(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "true")
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))
    assert c.has_service("mcp")


def test_mcp_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setenv("AUTONOMY_MODE", "supervised")
    c = _FakeContainer()
    asyncio.run(register_cli_tools(c))
    assert not c.has_service("mcp")


def test_cli_unavailable_tools_honors_browser_alias():
    """Sessions request the id 'browser'; the container serves it via the
    'browser_manager' service — honest-availability must not report it missing."""
    assert "browser" not in cli_unavailable_tools(["browser"])
    assert "browser_manager" in _CLI_REGISTERABLE_TOOLS
