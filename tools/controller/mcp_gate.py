"""Is there ANY MCP server this session could reach? If not, do not offer `mcp`.

Prod 72h to 2026-10-11: MCP had no server, yet every owner session got the `mcp`
tool with 13 dead actions. Two shapes:

* MCP off, or no configured server: ``MCPTool`` never builds a
  ``server_manager`` — nothing can load, not even a user's own server
  (``load_user_servers`` returns early without one).
* MCP on with a manager but no configured server: the tool is live only if THIS
  user has an enabled server of their own (``user_mcp_servers``, loaded by the
  orchestrator after the tool loads). That row is read here on every load, so a
  server the user adds later shows on the next session.

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
    if getattr(manager, "connections", None):
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
