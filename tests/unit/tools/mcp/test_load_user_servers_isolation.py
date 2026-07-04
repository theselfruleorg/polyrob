"""C6: MCPTool is a process-wide singleton; load_user_servers re-read the shared,
mutable self._current_user_id across awaits. A concurrent session's set_user_context
mid-load made the loop namespace/credential-fetch servers for the WRONG tenant.
load_user_servers must snapshot the user_id (passed explicitly by the orchestrator)
and use it consistently, regardless of concurrent mutation of the shared field.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tools.mcp.mcp_tool import MCPTool


def test_load_user_servers_snapshots_user_id_across_awaits():
    tool = MCPTool.__new__(MCPTool)
    tool.logger = logging.getLogger("t")
    tool._current_user_id = "userA"
    tool._loaded_users = set()
    tool.server_manager = AsyncMock()
    tool.server_manager.add_server = AsyncMock(return_value=True)

    config_calls = []

    class _Svc:
        async def get_user_servers(self, uid, enabled_only=True):
            return [SimpleNamespace(server_name="s1"), SimpleNamespace(server_name="s2")]

        async def get_server_config(self, uid, name):
            config_calls.append((uid, name))
            # Simulate a concurrent session hijacking the shared context mid-load.
            tool._current_user_id = "userB"
            await asyncio.sleep(0)
            return SimpleNamespace()  # truthy config

    tool._user_mcp_service = _Svc()

    asyncio.run(tool.load_user_servers(user_id="userA"))

    # Every credential fetch used the snapshot userA — never the mutated userB.
    assert config_calls == [("userA", "s1"), ("userA", "s2")], config_calls
    # Every server was namespaced to userA.
    added = [c.args[0] for c in tool.server_manager.add_server.call_args_list]
    assert added == ["user_userA::s1", "user_userA::s2"], added
    assert "userA" in tool._loaded_users


def test_unload_user_servers_uses_passed_user_id():
    tool = MCPTool.__new__(MCPTool)
    tool.logger = logging.getLogger("t")
    tool._current_user_id = "userB"  # shared field points elsewhere
    tool._loaded_users = {"userA"}
    tool.server_manager = AsyncMock()
    tool.server_manager.connections = {
        "user_userA::s1": object(),
        "user_userB::s9": object(),
    }
    tool.server_manager.disconnect_server = AsyncMock()

    asyncio.run(tool.unload_user_servers(user_id="userA"))

    # Only userA's server disconnected — NOT userB's (even though _current_user_id=userB).
    disconnected = [c.args[0] for c in tool.server_manager.disconnect_server.call_args_list]
    assert disconnected == ["user_userA::s1"], disconnected
    assert "userA" not in tool._loaded_users
