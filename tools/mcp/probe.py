"""Connect-test one MCP server config, and probe which transport a URL speaks (093 B1).

``/mcp add`` used to pick ``http`` for every URL not ending ``/sse``; a
spec-current Streamable HTTP server then refused it with 406. Now the add probes
:data:`tools.mcp.http_reply.PROBE_ORDER` (``streamable_http → http → sse``) and
stores the first type that answered ``initialize``. A failure names the HTTP
status and the first body line per transport — never a stack trace.

Tenant URLs only: every transport here is built with ``validate_ssrf=True``
(HTTPS, no internal address, the pinned connector). stdio is never built.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, List, Optional, Tuple

from tools.mcp.config import MCPServerConfig, MCPServerType
from tools.mcp.http_reply import PROBE_ORDER

#: Error prefixes the client/transport layers wrap around the real cause.
_WRAPPERS = ("Failed to connect MCP client: ", "MCP protocol error: ")


def clean_error(error: Any) -> str:
    """The cause in one line: wrapper prefixes, paths and tracebacks removed."""
    text = str(error or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in _WRAPPERS:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
    text = (text.splitlines() or [""])[0]
    text = re.sub(r'/[^\s:]+\.py', '[internal]', text)
    text = re.sub(r'File "[^"]+",', '', text)
    return (text[:240] + "...") if len(text) > 240 else text or "unknown error"


def _transport_for(config: MCPServerConfig, headers: Any, on_auth_refresh: Any) -> Optional[Any]:
    # Resolve the classes at call time (tests patch tools.mcp.protocol attributes).
    from tools.mcp import protocol as P
    kwargs = dict(url=config.url, headers=headers, timeout=min(config.timeout, 30),
                  validate_ssrf=True, on_auth_refresh=on_auth_refresh)  # SSRF: user-supplied URL
    if config.type == MCPServerType.SSE:
        return P.MCPSSETransport(**kwargs)
    if config.type == MCPServerType.HTTP:
        return P.MCPHTTPTransport(**kwargs)
    if config.type == MCPServerType.STREAMABLE_HTTP:
        return P.MCPStreamableHTTPTransport(**kwargs)
    return None


async def connect_test(config: MCPServerConfig, *, user_id: str = "",
                       server_name: str = "(unsaved)") -> Any:
    """Connect, ``initialize`` and list tools once; a ``TestConnectionResult``."""
    from tools.mcp import protocol as P
    from tools.mcp.oauth_bridge import apply_oauth_headers, make_auth_refresh_callback, oauth_applies_to
    from tools.mcp.user_mcp_service import TestConnectionResult

    client = None
    start = time.time()
    try:
        headers = await apply_oauth_headers(config, user_id, server_name)
        on_auth_refresh = (make_auth_refresh_callback(user_id, server_name)
                           if oauth_applies_to(config) else None)
        transport = _transport_for(config, headers, on_auth_refresh)
        if transport is None:
            return TestConnectionResult(success=False,
                                        error=f"Unsupported server type: {config.type}")
        client = P.MCPClient(transport)
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except asyncio.TimeoutError:
            return TestConnectionResult(success=False, error="MCP handshake timed out (30s)")
        tools = client.tools
        return TestConnectionResult(
            success=True,
            latency_ms=round((time.time() - start) * 1000, 2),
            tools_discovered=len(tools),
            tools=[t.get('name', 'unknown') for t in tools][:20],
        )
    except Exception as e:
        return TestConnectionResult(success=False, error=clean_error(e))
    finally:
        if client:
            try:
                await client.close()
            except Exception:
                pass


def _host_level(error: str) -> bool:
    """A failure every transport would hit the same way (DNS, refused, SSRF,
    timeout) — probing the next transport would only repeat it."""
    low = error.lower()
    return not low.startswith("http ") and any(k in low for k in (
        "network error", "cannot connect", "name or service", "nodename",
        "failed to initialize", "refused", "timed out", "ssrf", "not allowed"))


async def probe_transports(config: MCPServerConfig, *, user_id: str = "",
                           server_name: str = "(unsaved)",
                           order: Tuple[str, ...] = PROBE_ORDER) -> Tuple[Optional[str], Any, List[Tuple[str, str]]]:
    """Try each transport in ``order``; ``(type that answered, result, attempts)``.

    ``attempts`` is ``[(type, error), ...]`` for the transports that failed.
    Stops early on a host-level failure (see :func:`_host_level`)."""
    attempts: List[Tuple[str, str]] = []
    result = None
    for server_type in order:
        trial = config.model_copy(update={"type": MCPServerType(server_type)})
        result = await connect_test(trial, user_id=user_id, server_name=server_name)
        if getattr(result, "success", False):
            return server_type, result, attempts
        error = getattr(result, "error", None) or "unknown error"
        attempts.append((server_type, error))
        if _host_level(error):
            break
    return None, result, attempts


def probe_failure_text(attempts: List[Tuple[str, str]]) -> str:
    """``no transport answered initialize — streamable_http: HTTP 406: ...; http: ...``."""
    tried = "; ".join(f"{t}: {e}" for t, e in attempts) or "nothing tried"
    return f"no transport answered initialize — {tried}"
