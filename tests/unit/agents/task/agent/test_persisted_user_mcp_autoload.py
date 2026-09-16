"""A saved MCP server must load at session start without being asked for.

`SessionOrchestrator.initialize` only called `load_user_servers` when the
session's own `tools_config` carried a `"user:<name>"` entry in its MCP server
list. Nothing in the codebase ever PRODUCES such an entry — grep 2026-09-12
found the prefix consumed in two places and written in none — so the trigger
was a gate nobody could open. A persisted row existed and was never read.

The rule: if this session has a tenant and an MCP tool, the tenant's saved
servers load. `MCPTool.load_user_servers` is already tenant-scoped,
timeout-bounded, once-per-tenant cached and fail-open per server, so calling it
unconditionally costs nothing when there is nothing to load.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from agents.task.agent.orchestrator import SessionOrchestrator


def _orchestrator(user_id):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o.logger = logging.getLogger("t")
    o.user_id = user_id
    return o


class _FakeMCP:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def load_user_servers(self, user_id=None):
        self.calls.append(user_id)
        return self._result


def test_saved_servers_load_without_a_user_prefixed_request():
    mcp = _FakeMCP(SimpleNamespace(loaded_count=2, failed_count=0,
                                   failed_servers=[], timed_out=False))

    loaded = asyncio.run(_orchestrator("u1")._load_persisted_user_mcp_servers(mcp))

    assert mcp.calls == ["u1"], "the tenant must be passed explicitly (C6)"
    assert loaded == 2


def test_anonymous_session_loads_nothing():
    """No tenant means no tenant-scoped rows to read — and never a shared bucket."""
    mcp = _FakeMCP(SimpleNamespace(loaded_count=1, failed_count=0,
                                   failed_servers=[], timed_out=False))

    loaded = asyncio.run(_orchestrator("")._load_persisted_user_mcp_servers(mcp))

    assert mcp.calls == []
    assert loaded == 0


def test_a_tool_without_the_capability_is_skipped():
    loaded = asyncio.run(_orchestrator("u1")._load_persisted_user_mcp_servers(object()))
    assert loaded == 0


def test_a_raising_loader_never_breaks_session_start():
    class _Broken:
        async def load_user_servers(self, user_id=None):
            raise RuntimeError("mcp store is down")

    loaded = asyncio.run(_orchestrator("u1")._load_persisted_user_mcp_servers(_Broken()))
    assert loaded == 0


def test_legacy_int_return_is_honoured():
    """`load_user_servers` used to return a bare int; both shapes must count."""
    loaded = asyncio.run(_orchestrator("u1")._load_persisted_user_mcp_servers(_FakeMCP(3)))
    assert loaded == 3
