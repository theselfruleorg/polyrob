"""T0.4 — execute_mcp_tool must share the validated-execute core with execute_tool.

Before the fix, the controller-invoked ``execute_mcp_tool`` bypassed:
  1. requested_servers allowlist check
  2. empty-arguments detection
  3. _validate_and_convert_parameters (schema coercion)
  4. process_tool_result truncation / workspace offloading

After the fix, both entry points delegate to ``_execute_validated``.
"""
import inspect
import pytest
from tools.mcp.mcp_tool import MCPTool


def test_execute_validated_exists():
    """_execute_validated must exist as a method on MCPTool."""
    assert hasattr(MCPTool, "_execute_validated"), (
        "MCPTool must expose _execute_validated as the shared validated-execute core"
    )


def test_execute_validated_is_async():
    """_execute_validated must be an async method (it awaits server_manager.execute_tool)."""
    import asyncio
    assert asyncio.iscoroutinefunction(MCPTool._execute_validated), (
        "_execute_validated must be an async def"
    )


def test_execute_mcp_tool_routes_through_validated_core():
    """execute_mcp_tool (live controller path) must call _execute_validated."""
    src = inspect.getsource(MCPTool.execute_mcp_tool)
    assert "_execute_validated" in src, (
        "execute_mcp_tool must route through _execute_validated, "
        "not execute directly — this is the bypass that T0.4 closes"
    )


def test_execute_tool_routes_through_validated_core():
    """execute_tool (action path) must also call _execute_validated."""
    src = inspect.getsource(MCPTool.execute_tool)
    assert "_execute_validated" in src, (
        "execute_tool must delegate its inner execution to _execute_validated"
    )
