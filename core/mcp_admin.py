"""Owner-seat administration of per-tenant MCP servers — the ONE helper set.

Every owner surface (Telegram ``/mcp``, the REPL, the console) renders from
here, mirroring ``core/app_service/owner_ops.py``. Adding a second renderer is
how a surface ends up confidently wrong; extend this instead.

**Why this exists.** Until now the only way to give the agent a new MCP server
was to edit ``config/mcp_config.json`` or ``MCP_INSTALL_CATALOG_FILE`` on the
box. The owner's standing directive is that he can do anything from the chat,
and he is usually on a phone — so a capability reachable only from a shell is
one he does not have. When Aave shipped an MCP server, "install it" was a
support ticket to himself.

**What this is NOT.** It is not a second store and not a second policy. Every
rule still lives in :class:`tools.mcp.user_mcp_service.UserMCPService`:

* HTTPS only, no internal/loopback IPs (SSRF),
* **stdio refused** — a persisted stdio entry is a persisted arbitrary exec on
  the owner's box, which is the entire threat model for installing MCP servers
  (see ``tools/mcp/child_env.py``). This module refuses a command-shaped
  argument early so it reads as a policy, not as a URL typo,
* credentials encrypted at rest, per-tenant server cap, add rate limit.

**Who may call it.** An owner acting from an authenticated seat IS the operator
grant (034 §12). The agent cannot reach this module; its own install path is
``mcp_install``, which is separately gated, catalog-bound and approval-bound.
An empty ``user_id`` is refused — there is no shared bucket.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Argument shapes that mean "the owner is trying to add a local command", which
#: is exactly what must never be persisted. Checked before the store so the
#: refusal can explain itself.
_STDIO_MARKERS = ("npx", "uvx", "node", "python", "python3", "bunx", "deno", "sh", "bash")

_USAGE = (
    "Usage:\n"
    "  /mcp                      — the MCP servers I have\n"
    "  /mcp add <id> <https-url> [api-key]  — works from the next turn\n"
    "  /mcp remove <id>\n"
    "  /mcp test <id>            — connect and count the tools\n\n"
    "HTTPS servers only (Streamable HTTP, HTTP or SSE — the add finds which). "
    "A local command (npx/uvx/…) is refused: persisting one would persist "
    "arbitrary code execution on this box."
)


@dataclass(frozen=True)
class MCPAdminResult:
    ok: bool
    message: str


def resolve_user_mcp_service(container: Any = None) -> Optional[Any]:
    """The per-tenant MCP store, or None when this deployment has none.

    None is a real answer, not an error: the store is registered only when
    ``MCP_ENABLED`` is on and a database exists
    (``polyrob.app.initialization.initialize_user_mcp_service``).
    """
    try:
        if container is None:
            from core.container import DependencyContainer
            container = DependencyContainer._instance
        if container is None:
            return None
        return container.get_service("user_mcp_service")
    except Exception:
        return None


def _no_store_message() -> str:
    return ("The MCP server store is not available on this deployment, so there "
            "is nowhere to save a server. It needs POLYROB_OVERRIDES=MCP_ENABLED=true and a "
            "database. Nothing was changed.")


def _when(value: Any) -> str:
    """A stored timestamp as ``YYYY-MM-DD HH:MMZ`` (rows carry ISO text or datetime)."""
    if value is None or value == "":
        return ""
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text.replace("T", " ")[:16] + "Z"


def _last_probe(row: Any) -> str:
    """When the server was last probed (add/test/load), or ``never probed``.

    ``auth_status`` is ``connected``/``error`` only after a probe wrote it; the
    probe also stamps ``updated_at``."""
    if getattr(row, "auth_status", None) in ("connected", "error"):
        return _when(getattr(row, "updated_at", None)) or "probed"
    return "never probed"


def operator_server_lines(container: Any) -> List[str]:
    """The operator's static servers (repo defaults + ``<polyrob_home>/mcp.json``),
    with the live connection state when the ``mcp`` tool runs. Read-only — they
    change in the operator file, not from the chat."""
    cfg = getattr(getattr(container, "config", None), "mcp", None) or {}
    servers = cfg.get("servers") if isinstance(cfg, dict) else None
    if not isinstance(servers, dict) or not servers:
        return []
    manager = None
    for name in ("mcp", "mcp_tool"):
        try:
            if container.has_service(name):
                manager = getattr(container.get_service(name), "server_manager", None)
                break
        except Exception:
            pass
    conns = getattr(manager, "connections", None)
    conns = conns if isinstance(conns, dict) else {}
    lines = [f"Operator servers ({len(servers)}, set in the operator file):"]
    for name, sc in servers.items():
        sc = sc if isinstance(sc, dict) else {}
        kind = getattr(sc.get("type"), "value", sc.get("type")) or "?"
        where = sc.get("url") or " ".join(sc.get("command") or []) or ""
        conn = conns.get(name)
        if conn is None:
            state = "not connected in this process"
        else:
            status = getattr(getattr(conn, "status", None), "value", "?")
            state = f"{status} · {len(getattr(conn, 'tools', []) or [])} tools"
            if getattr(conn, "connected_at", None):
                from datetime import datetime, timezone
                state += " · connected " + _when(
                    datetime.fromtimestamp(conn.connected_at, timezone.utc).replace(tzinfo=None))
        lines.append(f"• {name} [{kind}] {where} · {state}")
        if conn is not None and getattr(conn, "last_error", None):
            lines.append(f"    last error: {conn.last_error}")
    return lines


async def perform_mcp_list(service: Any, user_id: str, *,
                           operator_lines: Sequence[str] = ()) -> MCPAdminResult:
    rows = await service.get_user_servers(user_id, enabled_only=False)
    lines: List[str] = []
    if not rows:
        lines.append("No MCP servers saved. Add one with:\n"
                     "  /mcp add aave https://mcp.aave.com")
    else:
        lines.append(f"MCP servers ({len(rows)}):")
    for r in rows:
        state = "on" if getattr(r, "enabled", True) else "off"
        kind = getattr(r, "server_type", None) or "?"
        tools = getattr(r, "tools_discovered", 0) or 0
        lines.append(f"• {r.server_name} [{kind}, {state}] {r.server_url}")
        lines.append(f"    {tools} tools · last probe: {_last_probe(r)}")
        err = getattr(r, "last_error", None)
        if err:
            lines.append(f"    last error: {err}")
    if operator_lines:
        lines.append("")
        lines.extend(operator_lines)
    return MCPAdminResult(True, "\n".join(lines))


async def perform_mcp_add(service: Any, user_id: str, server_id: str, url: str,
                          *, api_key: Optional[str] = None) -> MCPAdminResult:
    # 093 B1: probe streamable_http → http → sse; store the one that answered.
    result = await service.add_server(
        user_id, server_id, url,
        server_type="auto",
        api_key=api_key,
        auth_method="api_key" if api_key else "none",
        display_name=server_id,
        verify_connection=True,
    )
    if not getattr(result, "success", False):
        return MCPAdminResult(False, (
            f"Could not add '{server_id}': {getattr(result, 'error', None) or 'unknown error'}"))
    server = getattr(result, "server", None)
    kind = getattr(server, "server_type", None) or "auto"
    tools = getattr(result, "tools_discovered", None)
    tail = f", {tools} tools" if tools else ""
    return MCPAdminResult(True, (
        f"Added MCP server '{server_id}' ({kind}{tail}) — {url}\n"
        f"The agent can use it from the next turn (load_tool mcp)."))


async def perform_mcp_remove(service: Any, user_id: str, server_id: str) -> MCPAdminResult:
    removed = await service.delete_server(user_id, server_id)
    if not removed:
        return MCPAdminResult(False, f"No MCP server named '{server_id}' is saved.")
    return MCPAdminResult(True, (
        f"Removed MCP server '{server_id}'. The agent loses it from the next turn."))


async def perform_mcp_test(service: Any, user_id: str, server_id: str) -> MCPAdminResult:
    result = await service.test_connection(user_id, server_id)
    if not getattr(result, "success", False):
        return MCPAdminResult(False, (
            f"'{server_id}' did not answer: "
            f"{getattr(result, 'error', None) or 'unknown error'}"))
    count = getattr(result, "tools_discovered", None)
    latency = getattr(result, "latency_ms", None)
    bits = [f"'{server_id}' answered"]
    if latency is not None:
        bits.append(f"in {latency:.0f}ms")
    if count is not None:
        bits.append(f"· {count} tools")
    return MCPAdminResult(True, " ".join(bits))


async def mcp_reply_async(user_id: Optional[str], args: Sequence[str], *,
                          service: Any = None, container: Any = None) -> str:
    """Render one owner ``/mcp …`` command. AWAITED by every async seat
    (Telegram, the console, the REPL) on the loop that owns the live MCP manager.

    It used to run on a throwaway loop in a worker thread while the seat blocked
    the main loop on ``.result()``: the store's change listener then dropped
    connections from that foreign loop (a held manager lock deadlocked; aiohttp
    sessions said "attached to a different loop"), and ``/mcp add`` froze the
    whole process for up to three probes × 30 s."""
    if not user_id:
        return "Only the owner can manage MCP servers."

    args = list(args or [])
    verb = (args[0].lower() if args else "list")
    rest: List[str] = args[1:]
    if verb not in ("list", "add", "remove", "rm", "delete", "test", "help"):
        # `/mcp aave https://…` — treat a bare id+url as an add.
        verb, rest = "list", []
    if verb == "help":
        return _USAGE

    # Refuse a command-shaped add BEFORE touching the store, so the refusal can
    # explain the policy rather than surfacing a URL-validator message.
    if verb == "add" and len(rest) >= 2:
        target = rest[1]
        if (target.split("/")[0].lower() in _STDIO_MARKERS
                or not target.lower().startswith("http")):
            return ("MCP servers must be an https:// URL. A local command "
                    f"({target!r}) is refused: saving one would save arbitrary "
                    "code execution on this box.\n\n" + _USAGE)

    if service is None:
        service = resolve_user_mcp_service(container)
    if service is None:
        return _no_store_message()

    try:
        if verb == "list":
            if container is None:
                from core.container import DependencyContainer
                container = DependencyContainer._instance
            return (await perform_mcp_list(
                service, user_id, operator_lines=operator_server_lines(container))).message
        if verb == "add":
            if len(rest) < 2:
                return _USAGE
            api_key = rest[2] if len(rest) > 2 else None
            return (await perform_mcp_add(service, user_id, rest[0], rest[1],
                                          api_key=api_key)).message
        if verb in ("remove", "rm", "delete"):
            if not rest:
                return _USAGE
            return (await perform_mcp_remove(service, user_id, rest[0])).message
        if verb == "test":
            if not rest:
                return _USAGE
            return (await perform_mcp_test(service, user_id, rest[0])).message
    except Exception as exc:
        logger.warning("mcp admin verb failed", exc_info=True)
        return f"The MCP command did not run: {exc}"
    return _USAGE


def mcp_reply(user_id: Optional[str], args: Sequence[str], *,
              service: Any = None, container: Any = None) -> str:
    """Sync wrapper for a caller with NO running event loop (a script, a test).

    On a running loop it refuses rather than block it: await
    :func:`mcp_reply_async` there. A sync caller has no live MCP manager on its
    thread; the store's change listener hands any manager change to the tool's
    own loop (``tools/mcp/user_servers.py::schedule_store_change``)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(mcp_reply_async(user_id, args, service=service,
                                           container=container))
    raise RuntimeError("mcp_reply called on a running event loop; await mcp_reply_async")
