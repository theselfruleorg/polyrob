"""UP-01 Item 1 — the per-(user, server) MCP exec rate limit must also guard the
flattened ``{server}_{tool}`` direct-action path (``execute_mcp_tool``), not just
``execute_tool``. Previously only ``execute_tool`` checked, so the limit was
effectively unenforced on the path the LLM actually uses.

Uses a real limiter with an injected clock — no sleeps, no network.

T0.4 note: ``execute_mcp_tool`` now routes through ``_execute_validated``, which
applies ``process_tool_result`` serialisation before returning.  The result value
is therefore a JSON-encoded string (``'"ok"'``) rather than the raw Python object
(``'ok'``).  The assertions below use ``"ok" in result`` so they remain correct
regardless of how the serialisation wraps the value.
"""
import json
import logging
from unittest.mock import MagicMock

import pytest

from tools.mcp.mcp_tool import MCPTool
from tools.mcp.rate_limit import MCPExecRateLimiter
from core.exceptions import ToolError


class _FakeServerManager:
    def __init__(self):
        self.calls = 0
        # _execute_validated looks up self.server_manager.connections[server_name]
        # to find the schema; supply an empty dict so validation is skipped cleanly.
        self.connections = {}

    def get_all_tools(self):
        return {"anysite": {"search": {}}}

    async def execute_tool(self, server_name, tool_name, arguments):
        self.calls += 1
        return "ok"


async def _noop_rate_limit(key: str) -> None:
    """No-op stub for BaseTool.rate_limit — _execute_validated calls it but
    the bare tool has no _services, so stub it out for unit tests."""


def _bare_tool(limiter):
    """An MCPTool with only the attributes execute_mcp_tool / _execute_validated touch."""
    t = object.__new__(MCPTool)  # bypass BaseComponent.__init__
    t.logger = logging.getLogger("test.mcp")
    t._exec_rate_limiter = limiter
    t._current_user_id = "user1"
    t.server_manager = _FakeServerManager()
    # _execute_validated calls self.rate_limit() (BaseTool burst-rate helper);
    # the bare fixture has no _services, so stub it with a no-op coroutine.
    t.rate_limit = _noop_rate_limit
    # No requested_servers allowlist for these tests.
    t.requested_servers = None
    # container is a property on BaseComponent whose getter lazily calls
    # DependencyContainer.get_instance() when _container is falsy.
    # Provide a truthy stub so the property returns immediately without
    # hitting the singleton — get_service returns None so all 3 container
    # branches in _execute_validated skip their bodies (they guard on
    # `if orchestrator and ...`), exercising the production property path safely.
    stub_container = MagicMock()
    stub_container.get_service.return_value = None
    t._container = stub_container
    return t


@pytest.mark.asyncio
async def test_execute_mcp_tool_enforces_rate_limit():
    rl = MCPExecRateLimiter(max_calls=2, window_seconds=60)
    t = _bare_tool(rl)

    # First two calls go through.  _execute_validated JSON-serialises the raw
    # result via process_tool_result, so "ok" becomes '"ok"' — check containment.
    assert "ok" in await t.execute_mcp_tool("anysite_search", {"q": "a"})
    assert "ok" in await t.execute_mcp_tool("anysite_search", {"q": "b"})
    assert t.server_manager.calls == 2

    # Third within the window is rejected BEFORE reaching the server manager.
    with pytest.raises(ToolError) as ei:
        await t.execute_mcp_tool("anysite_search", {"q": "c"})
    assert "Rate limit reached" in str(ei.value)
    assert t.server_manager.calls == 2  # not incremented — blocked before execution


@pytest.mark.asyncio
async def test_rate_limit_is_per_server_bucket():
    rl = MCPExecRateLimiter(max_calls=1, window_seconds=60)
    t = _bare_tool(rl)
    # anysite gets its single slot...
    assert "ok" in await t.execute_mcp_tool("anysite_search", {})
    # ...but a different server has its own bucket. Register a second server.
    t.server_manager.get_all_tools = lambda: {"anysite": {"search": {}}, "ghost": {"publish": {}}}
    assert "ok" in await t.execute_mcp_tool("ghost_publish", {})
    # exhausting anysite again is blocked
    with pytest.raises(ToolError):
        await t.execute_mcp_tool("anysite_search", {})
