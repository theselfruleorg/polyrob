"""098 TS-03: per-user MCP — unload removes the connection; listings read live status."""
import asyncio
from types import SimpleNamespace

from tools.mcp.server_manager import MCPServerManager, ServerConnection
from tools.mcp.user_mcp_service import UserMCPService
from tools.mcp.user_servers import live_status


class _Client:
    transport = SimpleNamespace(is_closed=False)

    async def close(self):
        pass


def test_unload_removes_connections():
    async def go():
        from tools.mcp.mcp_tool import MCPTool
        mgr = MCPServerManager()
        for n in ("user_u1::a", "user_u1::b", "global"):
            mgr.connections[n] = ServerConnection(name=n, config=None, client=_Client())
        tool = MCPTool.__new__(MCPTool)
        tool.server_manager = mgr
        tool._current_user_id = "u1"
        tool._loaded_users = {"u1"}
        tool.logger = SimpleNamespace(debug=lambda *a: None, info=lambda *a: None,
                                      error=lambda *a: None)
        n = await tool.unload_user_servers("u1")
        assert n == 2 and list(mgr.connections) == ["global"]
    asyncio.run(go())


def test_listing_prefers_live_status():
    mgr = MCPServerManager()
    conn = ServerConnection(name="user_u1::a", config=None)
    mgr.connections["user_u1::a"] = conn
    tool = SimpleNamespace(server_manager=mgr)
    svc = UserMCPService.__new__(UserMCPService)
    row = SimpleNamespace(server_name="a", auth_status="connected")
    svc.live_status = lambda name: live_status(tool, name)
    assert svc._listing_status("u1", row) == "disconnected"  # live, not the stored probe
    other = SimpleNamespace(server_name="b", auth_status="connected")
    assert svc._listing_status("u1", other) == "last_probe:connected"
