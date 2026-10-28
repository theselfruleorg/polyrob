"""When the ``mcp`` tool is usable, and how a tenant's own servers reach it (093 B2).

Before 093 the tool was enabled only by the STATIC server list
(``config/mcp_config.json`` + the operator file), so a server the owner added
with ``/mcp add`` could never switch the tool on: no ``server_manager`` was
built, and ``load_user_servers`` returned early without one. The remedy text
(``core/tool_gates.py`` ``no-mcp-server``) promises "it works from the next
turn"; this module makes that true:

* :func:`has_reachable_server` — the ONE sync predicate behind
  ``MCPTool._enabled`` and ``MCPTool.availability_gap``: the flag is on AND
  (a static server OR a live connection OR an enabled server of any tenant in
  the ``user_mcp_servers`` store). The store side is the cached count
  ``UserMCPService.enabled_server_count``, refreshed at boot and on every
  add/remove/update in this process. ``tools/controller/mcp_gate.py`` then
  refines the answer per tenant at load time.
* :func:`sync_user_servers` — reconcile ``user_{uid}::<name>`` connections with
  the tenant's enabled rows on every load: connect a new row, reconnect a
  dropped one, drop a removed one. No once-per-tenant latch, so a server added
  mid-process is picked up on the next ``load_tool("mcp")`` — no restart.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UserServersLoadResult:
    """Result of loading user MCP servers (FIX #8)."""
    loaded_count: int = 0
    failed_count: int = 0
    failed_servers: List[Dict[str, str]] = field(default_factory=list)  # [{name, error}]
    timed_out: bool = False


def user_store(tool: Any) -> Optional[Any]:
    """The per-tenant store the tool reads: the one set on it, else the process singleton."""
    svc = getattr(tool, "_user_mcp_service", None)
    if svc is not None:
        return svc
    try:
        from tools.mcp.user_mcp_service import get_user_mcp_service
        return get_user_mcp_service()
    except Exception:
        return None


def _connections(manager: Any) -> Dict[str, Any]:
    conns = getattr(manager, "connections", None)
    return conns if isinstance(conns, dict) else {}


def has_static_server(mcp_config: Any) -> bool:
    servers = getattr(mcp_config, "servers", None) or {}
    if not isinstance(servers, dict):
        return False
    return any(getattr(cfg, "enabled", True) is not False for cfg in servers.values())


def user_prefix(user_id: Any) -> str:
    """The namespace of one tenant's servers in the shared manager."""
    return f"user_{user_id}::"


def is_user_server(name: str) -> bool:
    """A tenant's own server (``user_<uid>::<name>``), not a static/operator one."""
    name = str(name or "")
    return name.startswith("user_") and "::" in name


def server_visible(name: str, user_id: Optional[str]) -> bool:
    """May ``user_id`` see/use ``name``? Global (static/operator/installed) servers:
    yes. A tenant server: only its own tenant. ONE manager holds every tenant's
    servers (with their credentials), so every reader filters through here."""
    if not is_user_server(name):
        return True
    return bool(user_id) and str(name).startswith(user_prefix(user_id))


def _connected(conn: Any) -> bool:
    from tools.mcp.server_manager import ServerStatus
    return getattr(conn, "status", None) == ServerStatus.CONNECTED


def visible_connected(manager: Any, user_id: Optional[str]) -> Dict[str, Any]:
    """The CONNECTED connections ``user_id`` may use (global + its own)."""
    return {name: conn for name, conn in _connections(manager).items()
            if _connected(conn) and server_visible(name, user_id)}


def visible_tools(manager: Any, user_id: Optional[str]) -> Dict[str, Any]:
    """``manager.get_all_tools()`` filtered to what ``user_id`` may see (global +
    its own). Every agent-facing LISTING reads this, never the raw call: one
    manager holds every tenant's servers."""
    try:
        everything = manager.get_all_tools() or {}
    except Exception:
        return {}
    return {name: tools for name, tools in everything.items()
            if server_visible(name, user_id)}


def has_reachable_server(tool: Any) -> bool:
    """True when SOME MCP server could serve a session on this deploy.

    Flag off → False. A static server or a CONNECTED global connection (an
    installed server) → True; a tenant's live connection is not counted here
    (it is that tenant's alone). Else the store's cached count of enabled tenant
    servers: > 0 → True; 0 → False; an unknown count (a store read fault) →
    True, the gate's fail-open precedent — ``mcp_gate`` gives the per-tenant
    answer at load time."""
    if not getattr(tool, "_flag_on", False):
        return False
    if has_static_server(getattr(tool, "mcp_config", None)):
        return True
    if any(_connected(c) and not is_user_server(n)
           for n, c in _connections(getattr(tool, "server_manager", None)).items()):
        return True
    svc = user_store(tool)
    if svc is None:
        return False
    count = getattr(svc, "enabled_server_count", None)
    return count is None or count > 0


async def drop_user_server(tool: Any, user_id: str, server_name: str) -> None:
    """Change listener: a removed/changed tenant server leaves the live manager now."""
    manager = getattr(tool, "server_manager", None)
    name = f"user_{user_id}::{server_name}"
    if manager is not None and name in _connections(manager):
        try:
            await manager.remove_server(name)
        except Exception as e:
            tool.logger.warning(f"Could not drop MCP server {name}: {e}")


async def sync_user_servers(tool: Any, user_id: Optional[str],
                            overall_timeout: float = 60.0) -> UserServersLoadResult:
    """Reconcile this tenant's live connections with its enabled rows.

    C6: ``user_id`` is snapshotted once and used across every await, so a
    concurrent session mutating ``tool._current_user_id`` cannot make this
    namespace or fetch servers for the wrong tenant."""
    from tools.mcp.server_manager import ServerStatus

    result = UserServersLoadResult()
    log = tool.logger
    uid = user_id or getattr(tool, "_current_user_id", None)
    if not uid:
        log.debug("No user context set, skipping user server loading")
        return result
    svc = user_store(tool)
    if not svc:
        log.warning("UserMCPService not configured, cannot load user servers")
        return result
    manager = getattr(tool, "server_manager", None)
    if not manager:
        log.warning("Server manager not initialized, cannot load user servers")
        return result

    def _fail(server_name: str, error: str) -> None:
        result.failed_count += 1
        result.failed_servers.append({"name": server_name, "error": error})

    prefix = user_prefix(uid)
    try:
        async with asyncio.timeout(overall_timeout):
            rows = await svc.get_user_servers(uid, enabled_only=True)
            wanted = {f"{prefix}{r.server_name}": r for r in rows}
            for stale in [n for n in list(_connections(manager)) if n.startswith(prefix) and n not in wanted]:
                await manager.remove_server(stale)
                log.info(f"Dropped user MCP server no longer saved: {stale}")
            for name, row in wanted.items():
                existing = _connections(manager).get(name)
                if existing is not None:
                    if getattr(existing, "status", None) == ServerStatus.CONNECTED:
                        continue
                    ok = await manager.connect_server(name)
                    if ok:
                        result.loaded_count += 1
                    else:
                        _fail(row.server_name, getattr(existing, "last_error", None) or "reconnect failed")
                    continue
                config = await svc.get_server_config(uid, row.server_name)
                if not config:
                    continue
                try:
                    if await manager.add_server(name, config):
                        log.info(f"Added user MCP server: {name}")
                        result.loaded_count += 1
                    else:
                        conn = _connections(manager).get(name)
                        _fail(row.server_name, getattr(conn, "last_error", None)
                              or "Server manager rejected server (unknown reason)")
                        log.warning(f"Failed to add user MCP server: {name}")
                except Exception as e:
                    _fail(row.server_name, str(e))
                    log.error(f"Error adding user MCP server {name}: {e}")
            tool._loaded_users.add(uid)
            log.info(f"Loaded {result.loaded_count} user MCP servers for user {uid}"
                     f" ({result.failed_count} failed)")
            return result
    except asyncio.TimeoutError:
        result.timed_out = True
        log.error(f"Timeout loading user servers after {overall_timeout}s - "
                  f"loaded {result.loaded_count}, failed {result.failed_count}")
        return result
    except Exception as e:
        log.error(f"Failed to load user servers: {e}")
        return result


# --- the sessions that hold `mcp` (one shared tool, many sessions) --------------
#
# ``MCPTool`` is a process singleton; every session that loads it registers the
# flat ``{server}_{tool}`` actions on ITS controller. A store change must reach
# those controllers (an added server registers, a removed one unregisters), and
# one session's end must not unload the servers another session of the same
# tenant still uses. The links are weak: a controller that is gone without a
# cleanup stops counting by itself.

def _links(tool: Any) -> Dict[int, Any]:
    links = getattr(tool, "_controller_links", None)
    if links is None:
        links = {}
        try:
            tool._controller_links = links
        except Exception:
            pass
    return links


def attach_controller(tool: Any, controller: Any) -> None:
    """Record that ``controller`` (one session) holds the ``mcp`` tool."""
    import weakref
    links = _links(tool)
    key = id(controller)
    try:
        ref = weakref.ref(controller, lambda _r, k=key: links.pop(k, None))
    except TypeError:
        return
    links[key] = ref


def live_controllers(tool: Any, user_id: Optional[str]) -> List[Any]:
    """The live controllers of ``user_id`` that hold the tool."""
    out = []
    for ref in list(_links(tool).values()):
        ctrl = ref()
        if ctrl is not None and str(getattr(ctrl, "user_id", None)) == str(user_id):
            out.append(ctrl)
    return out


def release_controller(tool: Any, controller: Any, user_id: Optional[str] = None) -> int:
    """Forget ``controller`` and every controller of its session; return how many
    live controllers of the same tenant (other sessions) still hold the tool."""
    links = _links(tool)
    sid = getattr(controller, "session_id", None)
    uid = user_id or getattr(controller, "user_id", None)
    for key, ref in list(links.items()):
        ctrl = ref()
        if ctrl is None or ctrl is controller or (
                sid is not None and getattr(ctrl, "session_id", None) == sid):
            links.pop(key, None)
    return len(live_controllers(tool, uid))


async def reconcile_tenant(tool: Any, user_id: str, server_name: str, event: str) -> None:
    """Apply one store change to the live manager and the sessions that hold it.

    Runs on the tool's OWN loop only (see :func:`schedule_store_change`): the
    manager's locks, message-handler tasks and HTTP sessions belong to it."""
    if event in ("removed", "updated"):
        await drop_user_server(tool, user_id, server_name)
    controllers = live_controllers(tool, user_id)
    if not controllers:
        return  # no session holds `mcp`; the next load reconciles
    await sync_user_servers(tool, user_id)
    for ctrl in controllers:
        sync = getattr(ctrl, "_register_mcp_tools_as_direct_actions", None)
        if callable(sync):
            try:
                await sync(tool)
            except Exception as e:
                tool.logger.warning(f"MCP: re-registering a session's actions failed: {e}")


def schedule_store_change(tool: Any, user_id: str, server_name: str, event: str):
    """Hand a store change to the tool's own loop; never touch the manager here.

    * on the tool's loop: return the coroutine for the caller to await (the
      seat's reply then tells the truth: the server is already dropped);
    * a different running loop (a sync caller's throwaway loop, a worker
      thread): ``run_coroutine_threadsafe`` onto the tool's loop, NOT waited;
    * the tool's loop is not running (no manager, a test, shutdown): nothing —
      ``sync_user_servers`` drops an unsaved row on the next load.
    Returns an awaitable for the first case, else None."""
    loop = getattr(tool, "_loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        return None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        return reconcile_tenant(tool, user_id, server_name, event)
    asyncio.run_coroutine_threadsafe(
        reconcile_tenant(tool, user_id, server_name, event), loop)
    return None


async def tenant_server_rows(user_id: str, container: Any = None):
    """``(rows, error)`` — the tenant's saved servers for a read-only view.

    The store when this process has one; else the same table through the
    container's ``database_manager`` (the console is a separate process with a
    database but no MCP store). ``rows`` is None with a named ``error`` when
    nothing could be read — never an empty list that reads as "none saved"."""
    svc = None
    try:
        if container is not None and container.has_service("user_mcp_service"):
            svc = container.get_service("user_mcp_service")
    except Exception:
        svc = None
    try:
        if svc is not None:
            return list(await svc.get_user_servers(str(user_id), enabled_only=False)), None
        db = None
        if container is not None and container.has_service("database_manager"):
            db = container.get_service("database_manager")
        conn = getattr(db, "connection", None)
        if conn is None:
            return None, "no MCP server store and no database in this process"
        rows = await conn.fetch_all(
            "SELECT * FROM user_mcp_servers WHERE user_id = ? ORDER BY created_at DESC",
            (str(user_id),))
        return [_row_view(r) for r in rows or []], None
    except Exception as e:
        text = str(e)
        if "no such table" in text:
            return [], None  # the store was never created: nothing was ever saved
        return None, f"the MCP server store could not be read: {text[:160]}"


def _row_view(row: Any) -> Any:
    from types import SimpleNamespace
    get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k] if k in row.keys() else d)
    return SimpleNamespace(
        server_name=get("server_name"), server_url=get("server_url"),
        server_type=get("server_type"), enabled=bool(get("enabled", 1)),
        tools_discovered=get("tools_discovered", 0) or 0,
        auth_status=get("auth_status"), last_error=get("last_error"),
        updated_at=get("updated_at"))
