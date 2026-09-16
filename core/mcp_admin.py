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
    "  /mcp add <id> <https-url> [api-key]\n"
    "  /mcp remove <id>\n"
    "  /mcp test <id>            — connect and count the tools\n\n"
    "HTTP/SSE servers only. A local command (npx/uvx/…) is refused: persisting "
    "one would persist arbitrary code execution on this box."
)


@dataclass(frozen=True)
class MCPAdminResult:
    ok: bool
    message: str


def resolve_user_mcp_service(container: Any = None) -> Optional[Any]:
    """The per-tenant MCP store, or None when this deployment has none.

    None is a real answer, not an error: the store is registered only when
    ``MCP_ENABLED`` is on and a database exists
    (``core.initialization.initialize_user_mcp_service``).
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
            "is nowhere to save a server. It needs MCP_ENABLED=true and a "
            "database. Nothing was changed.")


async def perform_mcp_list(service: Any, user_id: str) -> MCPAdminResult:
    rows = await service.get_user_servers(user_id)
    if not rows:
        return MCPAdminResult(True, (
            "No MCP servers saved. Add one with:\n"
            "  /mcp add aave https://mcp.aave.com"))
    lines = [f"MCP servers ({len(rows)}):"]
    for r in rows:
        state = "on" if getattr(r, "enabled", True) else "off"
        tools = getattr(r, "tools_discovered", 0) or 0
        tail = f" · {tools} tools" if tools else ""
        lines.append(f"• {r.server_name} [{state}] {r.server_url}{tail}")
        err = getattr(r, "last_error", None)
        if err:
            lines.append(f"    last error: {err}")
    return MCPAdminResult(True, "\n".join(lines))


async def perform_mcp_add(service: Any, user_id: str, server_id: str, url: str,
                          *, api_key: Optional[str] = None) -> MCPAdminResult:
    server_type = "sse" if url.rstrip("/").endswith("/sse") else "http"
    result = await service.add_server(
        user_id, server_id, url,
        server_type=server_type,
        api_key=api_key,
        auth_method="api_key" if api_key else "none",
        display_name=server_id,
        verify_connection=True,
    )
    if not getattr(result, "success", False):
        return MCPAdminResult(False, (
            f"Could not add '{server_id}': {getattr(result, 'error', None) or 'unknown error'}"))
    return MCPAdminResult(True, (
        f"Added MCP server '{server_id}' ({server_type}) — {url}\n"
        f"It loads automatically at the start of every new session."))


async def perform_mcp_remove(service: Any, user_id: str, server_id: str) -> MCPAdminResult:
    removed = await service.delete_server(user_id, server_id)
    if not removed:
        return MCPAdminResult(False, f"No MCP server named '{server_id}' is saved.")
    return MCPAdminResult(True, (
        f"Removed MCP server '{server_id}'. Sessions already running keep it "
        f"until they end."))


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


def _run(coro):
    """Run ``coro`` whether or not the caller already holds an event loop."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


def mcp_reply(user_id: Optional[str], args: Sequence[str], *,
              service: Any = None, container: Any = None) -> str:
    """Render one owner ``/mcp …`` command. Sync — every seat calls this."""
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
            return _run(perform_mcp_list(service, user_id)).message
        if verb == "add":
            if len(rest) < 2:
                return _USAGE
            api_key = rest[2] if len(rest) > 2 else None
            return _run(perform_mcp_add(service, user_id, rest[0], rest[1],
                                        api_key=api_key)).message
        if verb in ("remove", "rm", "delete"):
            if not rest:
                return _USAGE
            return _run(perform_mcp_remove(service, user_id, rest[0])).message
        if verb == "test":
            if not rest:
                return _USAGE
            return _run(perform_mcp_test(service, user_id, rest[0])).message
    except Exception as exc:
        logger.warning("mcp admin verb failed", exc_info=True)
        return f"The MCP command did not run: {exc}"
    return _USAGE
