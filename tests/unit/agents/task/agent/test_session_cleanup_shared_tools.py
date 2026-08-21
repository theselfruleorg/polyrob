"""Regression tests for the 2026-08-21 X/Twitter prod outage: a session
eviction (orchestrator.cleanup(full_cleanup=True)) tore down CONTAINER-OWNED
tool singletons by calling tool._cleanup() directly.

Two invariants under test (the 2026-08-21 singleton-teardown incident):

1. Session teardown must NOT touch a tool instance that is owned by the
   DependencyContainer (or the shared BrowserManager) — those are process-wide
   singletons shared with every other session. One session's eviction nulled
   TwitterTool.client / MCPTool.server_manager for the whole process.

2. A genuinely session-owned tool must be torn down via its PUBLIC cleanup()
   (which updates _initialized), never via the private _cleanup() — the direct
   _cleanup() call skipped the only writer of _initialized, so
   load_tools_from_container's `if not tool.is_initialized` re-init gate never
   fired again and the tool stayed dead until process restart.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.task.session.cleanup import SessionCleanupMixin


class _FakeTool:
    """Mirrors BaseTool lifecycle semantics: public cleanup() flips the flag
    and delegates to _cleanup(); calling _cleanup() directly does not."""

    def __init__(self):
        self._initialized = True
        self.client = "live-client"
        self.public_cleanup_calls = 0
        self.private_cleanup_calls = 0

    @property
    def is_initialized(self):
        return self._initialized

    async def cleanup(self):
        self.public_cleanup_calls += 1
        await self._cleanup()
        self._initialized = False

    async def _cleanup(self):
        self.private_cleanup_calls += 1
        self.client = None


class _FakeContainer:
    """Container double: identity-resolvable services, like DependencyContainer."""

    def __init__(self, services):
        self._services = services

    def has_service(self, name):
        return name in self._services

    def get_service(self, name):
        return self._services.get(name)


class _FakeController:
    def __init__(self, tools):
        self._tools = tools

    def list_tools(self):
        return list(self._tools.keys())

    def get_tool(self, name):
        return self._tools.get(name)


class _Orchestrator(SessionCleanupMixin):
    def __init__(self, controller, container=None, browser=None):
        self.session_id = "sess-1"
        self.user_id = "user-1"
        self.agents = {}
        self._browser_contexts = set()
        self.browser_manager = MagicMock()
        self.browser_manager.release_context = AsyncMock()
        self.browser_manager.browser = browser
        self.controller = controller
        self.container = container
        self.session_manager = None
        self.logger = MagicMock()


@pytest.mark.asyncio
async def test_full_cleanup_skips_container_owned_singleton_tools():
    """A tool whose instance IS the container's registered service must
    survive session eviction untouched (client intact, still initialized)."""
    twitter = _FakeTool()
    mcp = _FakeTool()
    container = _FakeContainer({"twitter_tool": twitter, "mcp": mcp})
    controller = _FakeController({"twitter": twitter, "mcp": mcp})
    orch = _Orchestrator(controller, container=container)

    await orch.cleanup(full_cleanup=True)

    for tool in (twitter, mcp):
        assert tool.client == "live-client", "container singleton was destroyed by session cleanup"
        assert tool.is_initialized is True
        assert tool.public_cleanup_calls == 0
        assert tool.private_cleanup_calls == 0


@pytest.mark.asyncio
async def test_full_cleanup_skips_shared_browser_instance():
    """The browser resolved from the shared BrowserManager must not be torn
    down by a session (docstring: 'does NOT cleanup the shared BrowserManager')."""
    browser = _FakeTool()
    controller = _FakeController({"browser": browser})
    orch = _Orchestrator(controller, container=_FakeContainer({}), browser=browser)

    await orch.cleanup(full_cleanup=True)

    assert browser.client == "live-client"
    assert browser.public_cleanup_calls == 0
    assert browser.private_cleanup_calls == 0


@pytest.mark.asyncio
async def test_session_owned_tool_torn_down_via_public_cleanup():
    """A tool NOT in the container is session-owned: it must be released via
    the public cleanup() so _initialized flips to False and a later
    load_tools_from_container re-initializes it."""
    owned = _FakeTool()
    controller = _FakeController({"scratch": owned})
    orch = _Orchestrator(controller, container=_FakeContainer({}))

    await orch.cleanup(full_cleanup=True)

    assert owned.public_cleanup_calls == 1
    assert owned.client is None
    assert owned.is_initialized is False, (
        "teardown bypassed cleanup(): flag says initialized over a dead tool"
    )


@pytest.mark.asyncio
async def test_session_owned_teardown_never_calls_private_cleanup_directly():
    """The ladder must not reach for _cleanup() when there is no public
    cleanup(): a private-half call skips the flag bookkeeping entirely."""

    class _PrivateOnlyTool:
        def __init__(self):
            self._initialized = True
            self.client = "live-client"
            self.private_calls = 0
            self.closed = 0

        @property
        def is_initialized(self):
            return self._initialized

        async def _cleanup(self):
            self.private_calls += 1
            self.client = None

        async def close(self):
            self.closed += 1
            self.client = None
            self._initialized = False

    tool = _PrivateOnlyTool()
    controller = _FakeController({"scratch": tool})
    orch = _Orchestrator(controller, container=_FakeContainer({}))

    await orch.cleanup(full_cleanup=True)

    assert tool.private_calls == 0, "_cleanup() must never be called from outside the class"
    assert tool.closed == 1
    assert tool.is_initialized is False
