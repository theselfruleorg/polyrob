"""h_mcp.py — the ``/mcp`` slash-command handler for the POLYROB CLI.

Read-only: list the configured MCP servers and their connection state. Prefers a
live ``MCPServerManager`` (via the loaded MCP tool on the agent controller, or a
container service) and falls back to the static config
(``config/mcp_config.json`` + the file-first ``~/.polyrob``/``./.polyrob`` overlay)
when no live manager is reachable. It never connects to or mutates anything.

The main REPL session wires registration (this module only exports the handler).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Live-manager discovery (best-effort, fail-open)
# ---------------------------------------------------------------------------


def _has_zero_arg_list_servers(obj: Any) -> bool:
    """True if ``obj.list_servers`` is callable with no required args.

    Guards against handing the MCPTool (whose ``list_servers(self, params)`` REQUIRES
    a params arg) to ``await manager.list_servers()`` — that raises TypeError and, even
    though it's caught, silently skips the live view when the tool is present but its
    manager isn't wired. The zero-arg ``MCPServerManager.list_servers()`` passes.
    """
    import inspect

    fn = getattr(obj, "list_servers", None)
    if not callable(fn):
        return False
    try:
        for p in inspect.signature(fn).parameters.values():
            if p.default is inspect.Parameter.empty and p.kind in (
                inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD
            ):
                return False
        return True
    except (ValueError, TypeError):
        return False


def _manager_from_service(svc: Any) -> Optional[Any]:
    """Extract an ``MCPServerManager``-like object from a service/tool, or None."""
    if svc is None:
        return None
    # The MCPTool holds its manager on ``.server_manager``.
    mgr = getattr(svc, "server_manager", None)
    if mgr is not None and _has_zero_arg_list_servers(mgr):
        return mgr
    # Or the service itself may be a manager (but NOT the MCPTool — see the guard).
    if _has_zero_arg_list_servers(svc):
        return svc
    return None


def _resolve_manager(ctx: Any) -> Optional[Any]:
    """Find a live ``MCPServerManager`` reachable from *ctx*, or ``None``.

    Tries the loaded MCP tool on the agent controller first (the REPL path),
    then a handful of container service names. Entirely fail-open.
    """
    # 1) The MCP tool loaded on the agent's controller (REPL live path).
    try:
        controller = getattr(ctx.agent, "controller", None)
        tools = getattr(controller, "_tools", None)
        if isinstance(tools, dict) and "mcp" in tools:
            mgr = _manager_from_service(getattr(tools["mcp"], "instance", None))
            if mgr is not None:
                return mgr
    except Exception:
        pass

    # 2) A container-registered MCP service.
    container = getattr(ctx, "container", None)
    get = getattr(container, "get_service", None)
    if callable(get):
        for name in ("mcp", "mcp_tool", "mcp_service"):
            try:
                svc = get(name)
            except Exception:
                svc = None
            mgr = _manager_from_service(svc)
            if mgr is not None:
                return mgr
    return None


# ---------------------------------------------------------------------------
# Static-config fallback
# ---------------------------------------------------------------------------


def _repo_config_path() -> Path:
    """Best-effort path to ``config/mcp_config.json`` (repo-root, then CWD)."""
    # cli/ui/commands/h_mcp.py -> parents[3] == repo root.
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "config" / "mcp_config.json"
    if candidate.exists():
        return candidate
    return Path.cwd() / "config" / "mcp_config.json"


def _load_static_config() -> Tuple[bool, Dict[str, Any]]:
    """Return ``(enabled, servers)`` from the static config, fail-open.

    ``servers`` maps server name -> its config dict. Merges the file-first local
    overlay (``~/.polyrob/mcp.json`` + ``./.polyrob/mcp.json``, project wins) on
    top of ``config/mcp_config.json``.
    """
    enabled = False
    servers: Dict[str, Any] = {}

    path = _repo_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            enabled = bool(data.get("enabled", False))
            raw = data.get("servers") or {}
            if isinstance(raw, dict):
                servers.update(raw)
        except Exception:
            pass

    # File-first local overlay (single-user mode) — presence also enables MCP.
    try:
        from tools.mcp.config import load_local_mcp_servers

        local = load_local_mcp_servers()
        if isinstance(local, dict) and local:
            servers.update(local)
            enabled = True
    except Exception:
        pass

    return enabled, servers


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _emit_live(ctx: Any, servers: List[Dict[str, Any]]) -> None:
    if not servers:
        ctx.emit("No MCP servers configured.", title="mcp")
        return
    lines = [f"MCP servers ({len(servers)} live):"]
    for s in servers:
        name = s.get("name", "?")
        status = s.get("status", "unknown")
        enabled = s.get("enabled", True)
        tools = s.get("tools_count")
        state = status if enabled else f"{status} (disabled)"
        tail = f" · {tools} tools" if isinstance(tools, int) else ""
        err = s.get("last_error")
        err_tail = f" · error: {err}" if err else ""
        lines.append(f"  {name:<20} {state}{tail}{err_tail}")
    ctx.emit("\n".join(lines), title="mcp")


def _emit_config(ctx: Any, enabled: bool, servers: Dict[str, Any]) -> None:
    if not enabled and not servers:
        ctx.emit("MCP disabled.", title="mcp")
        return
    if not servers:
        ctx.emit("No MCP servers configured.", title="mcp")
        return
    header = "MCP servers (from config):"
    if not enabled:
        header = "MCP disabled — configured servers:"
    lines = [header]
    for name, cfg in servers.items():
        cfg = cfg if isinstance(cfg, dict) else {}
        srv_enabled = cfg.get("enabled", True)
        stype = cfg.get("type", "?")
        state = "enabled" if srv_enabled else "disabled"
        lines.append(f"  {name:<20} {state} · type={stype} · not connected")
    ctx.emit("\n".join(lines), title="mcp")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def h_mcp(ctx: Any) -> None:
    """``/mcp`` (or ``/mcp list``) — list configured MCP servers + status.

    Read-only. Prefers a live ``MCPServerManager``; degrades to the static
    config when none is reachable.
    """
    sub = (ctx.args[0].lower() if getattr(ctx, "args", None) else "list")
    if sub not in ("", "list"):
        ctx.emit(f"Usage: /mcp [list]  (unknown subcommand '{sub}')", title="mcp")
        return

    manager = _resolve_manager(ctx)
    if manager is not None:
        try:
            servers = await manager.list_servers()
            if isinstance(servers, list):
                _emit_live(ctx, servers)
                return
        except Exception:
            # Fall through to the static-config view on any live failure.
            pass

    enabled, servers = _load_static_config()
    _emit_config(ctx, enabled, servers)
