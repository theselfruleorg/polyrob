"""Per-(user, server) MCP tool-execution rate limiter (WS-B3).

A sliding-window limiter keyed by an arbitrary hashable (typically
``(user_id, server_name)``) so a single agent/user cannot hammer expensive MCP
tools (crawl/scrape). Distinct from the per-tool throttle in ``BaseTool.rate_limit``
(global, service-dependent) and the add_server limiter in ``user_mcp_service``.

The clock is injectable (``time_fn``) so behavior is testable without real sleeps.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Hashable, List


class MCPExecRateLimiter:
    def __init__(
        self,
        max_calls: int = 20,
        window_seconds: int = 60,
        time_fn: Callable[[], float] = time.time,
    ):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._time_fn = time_fn
        self._calls: Dict[Hashable, List[float]] = {}

    def _prune(self, key: Hashable, now: float) -> List[float]:
        cutoff = now - self.window_seconds
        kept = [t for t in self._calls.get(key, []) if t > cutoff]
        self._calls[key] = kept
        return kept

    def check(self, key: Hashable) -> bool:
        """Return True and record the call if under the limit, else False."""
        now = self._time_fn()
        kept = self._prune(key, now)
        if len(kept) >= self.max_calls:
            return False
        kept.append(now)
        return True

    def retry_after(self, key: Hashable) -> float:
        """Seconds until the next slot frees (0 if a slot is available now)."""
        now = self._time_fn()
        kept = self._prune(key, now)
        if len(kept) < self.max_calls:
            return 0.0
        oldest = min(kept)
        return max(0.0, (oldest + self.window_seconds) - now)
