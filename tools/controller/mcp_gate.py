"""Is there ANY MCP server this session could reach? If not, do not offer `mcp`.

Prod 72h to 2026-10-11: MCP had no server, yet every owner session got the `mcp`
tool with 13 dead actions. Two shapes:

* MCP off: ``MCPTool`` builds no ``server_manager`` — nothing can load.
* MCP on with no static server: the manager exists (093 B2) and the tool is
  live only if THIS user has an enabled server of their own
  (``user_mcp_servers``). That row is read here on every load, and
  :func:`prepare_user_servers` connects it before the flat actions register, so
  a server the owner adds with ``/mcp add`` works on the NEXT TURN — no restart.

``MCPTool.availability_gap`` (the catalog's sync answer) agrees: it reads the
same store through its cached count of enabled servers.

An unreadable user store keeps the tool (fail-open): hiding a user's own server
on a read fault is worse than 13 idle actions.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

NO_SERVER_REASON = (
    "gated:unavailable-on-this-deploy — MCP is off or has no configured server "
    "(MCP_ENABLED + an mcp server entry, or a server of your own); the owner "
    "configures one, the agent cannot")


async def mcp_unreachable_reason(tool: Any, container: Any, user_id: Optional[str]) -> Optional[str]:
    """The gap reason when no MCP server is reachable for ``user_id``, else None."""
    manager = getattr(tool, "server_manager", None)
    if not manager:
        return NO_SERVER_REASON
    # One manager holds every tenant's servers: only a CONNECTED global server
    # or one of THIS tenant's counts — another tenant's connection does not.
    from tools.mcp.user_servers import visible_connected
    if visible_connected(manager, str(user_id) if user_id else None):
        return None
    if not user_id:
        return NO_SERVER_REASON
    try:
        svc = None
        if container is not None and container.has_service("user_mcp_service"):
            svc = container.get_service("user_mcp_service")
        if svc is None:
            return NO_SERVER_REASON
        rows = await svc.get_user_servers(str(user_id), enabled_only=True)
    except Exception:
        logger.debug("mcp gate: user server probe failed (fail-open)", exc_info=True)
        return None
    return None if rows else NO_SERVER_REASON


async def prepare_user_servers(tool: Any, container: Any, user_id: Optional[str]) -> None:
    """Connect ``user_id``'s saved servers before the flat actions register.

    A mid-session ``load_tool("mcp")`` used to register only the static servers'
    actions: the tenant's rows were loaded by the orchestrator at session start
    only. Fail-open — a store fault leaves the static servers."""
    if not user_id or not hasattr(tool, "load_user_servers"):
        return
    try:
        if getattr(tool, "_user_mcp_service", None) is None and container is not None \
                and container.has_service("user_mcp_service") and hasattr(tool, "set_user_mcp_service"):
            tool.set_user_mcp_service(container.get_service("user_mcp_service"))
        await tool.load_user_servers(user_id=str(user_id))
    except Exception:
        logger.warning("mcp: loading the user's saved servers failed", exc_info=True)
