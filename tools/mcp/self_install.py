"""Agent-initiated MCP install (P0-C).

Ties together: allowlist check (MCPCatalog) → exfiltration/injection screen (reusing the
memory threat scanner) → approval gate → MCPServerManager.add_server + config persist.
Gated OFF by default (MCP_SELF_INSTALL_ENABLED). The server runs OUT of process, so this
is the lowest-risk "install its own tools" rung. ``screen_config`` is pure (no I/O). No
action closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def self_install_enabled() -> bool:
    from core.env import bool_env
    return bool_env("MCP_SELF_INSTALL_ENABLED", False)


def screen_config(cfg: Dict[str, Any]) -> Optional[str]:
    """Return a rejection reason if ``cfg`` looks injected/exfiltrating, else None.

    Serialises the whole config and runs the shared instruction-override scanner over it
    (catch a prompt-injection smuggled through a description/arg), plus conservative
    structural checks (no remote-script-piped-to-shell stdio command).
    """
    try:
        blob = json.dumps(cfg, default=str)
    except Exception:
        return "config is not JSON-serialisable"
    from modules.memory.task.threat_scan import is_suspicious, has_invisible_unicode
    if is_suspicious(blob):
        return "config rejected: contains instruction-override / prompt-injection text"
    if has_invisible_unicode(blob):
        return "config rejected: contains invisible/bidi control characters"
    cmd = cfg.get("command")
    if isinstance(cmd, (list, tuple)):
        joined = " ".join(str(c) for c in cmd).lower()
        for bad in ("curl", "wget", "| sh", "|sh", "| bash", "|bash"):
            if bad in joined:
                return f"config rejected: stdio command looks like a remote-exec ({bad!r})"
    return None


def _entry_to_config_dict(entry) -> Dict[str, Any]:
    d: Dict[str, Any] = {"transport": entry.transport, "description": entry.description, "trust": entry.trust}
    if entry.url:
        d["url"] = entry.url
    if entry.command:
        d["command"] = list(entry.command)
    return d


async def perform_mcp_install(
    server_id: str,
    *,
    catalog,
    server_manager,
    approve,
    persist=None,
    context: Any = None,
) -> Tuple[bool, str]:
    """Install a catalog MCP server: gate → allowlist → screen → approve → add_server → persist.

    ``approve`` is an async callable ``(action_name, params, context) -> bool`` (the
    approval provider). Returns ``(ok, message)``.
    """
    if not self_install_enabled():
        return False, "MCP self-install is disabled (set MCP_SELF_INSTALL_ENABLED=true)."
    if not catalog.is_allowed(server_id):
        return False, f"'{server_id}' is not in the MCP install allowlist ({catalog.ids()})."
    entry = catalog.get(server_id)
    if entry is None:
        return False, f"no catalog entry for '{server_id}'."

    cfg_dict = _entry_to_config_dict(entry)
    reason = screen_config(cfg_dict)
    if reason:
        return False, reason

    try:
        allowed = await approve("mcp_install", {"server_id": server_id, **cfg_dict}, context)
    except Exception as e:
        return False, f"approval error for mcp_install: {e}"
    if not allowed:
        return False, f"mcp_install of '{server_id}' was not approved."

    from tools.mcp.config import MCPServerConfig, MCPServerType
    type_map = {
        "sse": MCPServerType.SSE,
        "http": getattr(MCPServerType, "HTTP", MCPServerType.SSE),
        "stdio": MCPServerType.STDIO,
    }
    config = MCPServerConfig(
        type=type_map.get(entry.transport, MCPServerType.SSE),
        url=entry.url,
        command=list(entry.command) if entry.command else None,
        enabled=True,
    )
    ok = await server_manager.add_server(server_id, config)
    if not ok:
        return False, f"MCP server '{server_id}' failed to install/connect."
    if persist is not None:
        try:
            persist(server_id, cfg_dict)
        except Exception as e:
            logger.warning("mcp_install persist failed for %s: %s", server_id, e)
    return True, f"Installed MCP server '{server_id}' ({entry.description})."
