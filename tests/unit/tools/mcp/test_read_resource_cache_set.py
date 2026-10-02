"""2026-09-22 harness/cache review F27: ``CacheManager.set`` takes a ``ttl``.

Both ``tools/mcp/mcp_tool.py::read_resource`` and
``tools/filesystem_docproc.py::_cache_result`` passed ``ttl=`` to a ``set``
that did not accept it. The MCP site re-raised the TypeError as a ToolError,
so EVERY successful resource read failed for the agent while caching was on.
"""
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.memory.cache_manager import CacheManager


def _cache(max_size=1000):
    cfg = SimpleNamespace(cache_size=max_size)
    c = CacheManager("cache_manager", cfg)
    c._initialized = True
    return c


def test_set_accepts_a_ttl_keyword():
    params = inspect.signature(CacheManager.set).parameters
    assert "ttl" in params and params["ttl"].default is None


def test_two_arg_set_is_unchanged():
    c = _cache()
    asyncio.run(c.set("k", {"a": 1}))
    assert asyncio.run(c.get("k")) == {"a": 1}
    assert "k" not in c._expiry


def test_ttl_entry_expires_on_read(monkeypatch):
    import modules.memory.cache_manager as cm
    now = [1000.0]
    monkeypatch.setattr(cm.time, "monotonic", lambda: now[0])
    c = _cache()
    asyncio.run(c.set("k", "v", ttl=300))
    assert asyncio.run(c.get("k")) == "v"
    now[0] += 301
    assert asyncio.run(c.get("k")) is None
    assert "k" not in c._cache and "k" not in c._expiry


def test_eviction_and_delete_drop_the_expiry_row():
    c = _cache(max_size=1)
    asyncio.run(c.set("a", 1, ttl=10))
    asyncio.run(c.set("b", 2, ttl=10))
    assert "a" not in c._expiry and "b" in c._expiry
    asyncio.run(c.delete("b"))
    assert c._expiry == {}


def test_mcp_read_resource_returns_content_with_caching_on():
    from tools.mcp.mcp_tool import MCPTool
    from tools.mcp.views import MCPReadResourceAction

    t = MCPTool.__new__(MCPTool)
    t._enabled = True
    t.requested_servers = ["srv"]
    t.ensure_initialized = AsyncMock()
    t.rate_limit = AsyncMock()
    t.logger = MagicMock()
    t.mcp_config = SimpleNamespace(enable_resource_caching=True, cache_ttl_seconds=300)
    t.cache_manager = _cache()
    t.server_manager = AsyncMock()
    t.server_manager.read_resource = AsyncMock(return_value={"contents": [{"text": "hello"}]})

    async def _run():
        params = MCPReadResourceAction(server_name="srv", resource_uri="file:///x")
        return await t.read_resource(params)

    out = asyncio.run(_run())
    assert out.uri == "file:///x"
    cached = asyncio.run(t.cache_manager.get("mcp_resource_srv_file:///x"))
    assert cached and cached["uri"] == "file:///x"
    assert "mcp_resource_srv_file:///x" in t.cache_manager._expiry


def test_mcp_read_resource_survives_a_cache_write_failure():
    from tools.mcp.mcp_tool import MCPTool
    from tools.mcp.views import MCPReadResourceAction

    t = MCPTool.__new__(MCPTool)
    t._enabled = True
    t.requested_servers = ["srv"]
    t.ensure_initialized = AsyncMock()
    t.rate_limit = AsyncMock()
    t.logger = MagicMock()
    t.mcp_config = SimpleNamespace(enable_resource_caching=True, cache_ttl_seconds=300)
    t.cache_manager = AsyncMock()
    t.cache_manager.get = AsyncMock(return_value=None)
    t.cache_manager.set = AsyncMock(side_effect=RuntimeError("cache is gone"))
    t.server_manager = AsyncMock()
    t.server_manager.read_resource = AsyncMock(return_value={"contents": [{"text": "hello"}]})

    async def _run():
        return await t.read_resource(MCPReadResourceAction(server_name="srv", resource_uri="file:///x"))

    out = asyncio.run(_run())
    assert out.uri == "file:///x"
    t.logger.warning.assert_called()


def test_docproc_declares_its_cache_ttl():
    from tools.filesystem_docproc import DocProcessingMixin
    assert isinstance(DocProcessingMixin.cache_ttl, int) and DocProcessingMixin.cache_ttl > 0
