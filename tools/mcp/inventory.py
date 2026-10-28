"""The read-only MCP inventory a console panel shows (093 review).

The console panel used to read ``load_local_mcp_servers()`` under the CONSOLE
process's home (on prod a different user, so not the agent's file), never listed
the servers the owner added with ``/mcp add``, and showed the registration gate
as the state ("flag on" read as "on" while zero servers were configured — the
exact error 093 fixed for the agent). This module answers from the same sources
the agent uses:

* the tenant's own servers: the user MCP store (``user_servers.tenant_server_rows``);
* the operator's servers: the live ``mcp`` tool's config when this process runs
  the agent, else the operator file — and a file this process cannot read is
  SAID, never rendered as an empty list;
* the state: ``tools.gate_effective.tool_state`` (the catalog's own answer when a
  container holds the tool).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional, Tuple


def operator_servers(container: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(servers, note)`` — the operator's servers as the AGENT sees them."""
    tool = None
    try:
        for name in ("mcp", "mcp_tool"):
            if container is not None and container.has_service(name):
                tool = container.get_service(name)
                break
    except Exception:
        tool = None
    servers = getattr(getattr(tool, "mcp_config", None), "servers", None)
    if isinstance(servers, dict):
        return {n: (c.model_dump() if hasattr(c, "model_dump") else c)
                for n, c in servers.items()}, None
    from tools.mcp.config import load_local_mcp_servers
    from tools.mcp.operator_home import operator_mcp_file
    path = operator_mcp_file()
    try:
        if not path.exists():
            return {}, (f"no operator file at {path} in the console process — the agent "
                        "may run under another home; /mcp lists the agent's servers")
        json.loads(path.read_text())
    except Exception as exc:
        return {}, (f"the operator file {path} is not readable from the console "
                    f"process ({type(exc).__name__}); /mcp lists the agent's servers")
    return load_local_mcp_servers(), None


def mcp_inventory(tenant: Optional[tuple], container: Any, *,
                  detail: Callable[[dict], Any]) -> dict:
    """``{"items", "error", "enabled", "state", "why"}`` for the console panel.

    ``tenant`` is ``(rows, error)`` (None = not read); ``detail`` renders a server
    config without its secrets (the console's scrubber)."""
    from tools.gate_effective import tool_state
    from tools.mcp.config import mcp_gate_on  # noqa: F401 — registers the gate
    state, why = tool_state("mcp", "MCP_ENABLED", None, container=container)
    enabled = state == "ON"
    operator, note = operator_servers(container)
    rows, rows_error = tenant if tenant is not None else (
        None, "your own servers were not read")
    errors = [e for e in (rows_error, note) if e]
    items = []
    for r in rows or []:
        kind = getattr(r, "server_type", None) or "?"
        what = detail({"url": getattr(r, "server_url", None)}) or ""
        items.append({"id": getattr(r, "server_name", ""), "kind": "mcp",
                      "what": f"{what} [{kind}]".strip(),
                      "on": enabled and bool(getattr(r, "enabled", True)),
                      "source": "mcp", "last_used": None})
    for name, cfg in (operator or {}).items():
        off = isinstance(cfg, dict) and cfg.get("enabled") is False
        items.append({"id": name, "kind": "mcp",
                      "what": detail(cfg) if isinstance(cfg, dict) else None,
                      "on": enabled and not off, "source": "mcp", "last_used": None})
    return {"items": items, "error": "; ".join(errors) or None,
            "enabled": enabled, "state": state, "why": why}
