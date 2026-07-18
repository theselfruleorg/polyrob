"""Per-(user, server) MCP tool-execution rate limiter (WS-B3) — back-compat shim.

F-1 (2026-07-17): the generic sliding-window primitive this module used to define
now lives in ``core/rate_limit.py::SlidingWindowLimiter``; this subclass only
preserves the established name and defaults for MCP-tier callers. Keyed by an
arbitrary hashable (typically ``(user_id, server_name)``) so a single agent/user
cannot hammer expensive MCP tools (crawl/scrape). Distinct from the per-tool
throttle in ``BaseTool.rate_limit`` (global, service-dependent) and the
add_server limiter in ``user_mcp_service``. The clock is injectable (``time_fn``).
"""
from core.rate_limit import SlidingWindowLimiter


class MCPExecRateLimiter(SlidingWindowLimiter):
    """Back-compat name for the MCP exec throttle. Prefer ``core.rate_limit``."""
