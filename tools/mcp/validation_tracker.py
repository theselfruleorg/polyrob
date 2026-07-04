"""MCP validation-failure tracking — schema-injection policy.

Extracted from ToolCallTracker (PR11). Repeated MCP-tool validation failures
should escalate to injecting the tool's full schema to help the LLM. That is
MCP error *policy*, not tool-call ID lifecycle, so it lives here in tools/mcp/
rather than on the generic tracker. ToolCallTracker composes one of these and
delegates to it, preserving its public method names for existing callers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, Optional


class MCPValidationTracker:
    """Per-(server, tool) validation-failure counter with TTL expiry.

    After ``failure_threshold`` failures within ``failure_ttl_minutes``, callers
    should inject the full tool schema. A successful call clears the counter.
    Self-contained and thread-safe (its own lock).
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        failure_threshold: int = 2,
        failure_ttl_minutes: int = 30,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self._failures: Dict[str, int] = {}
        self._timestamps: Dict[str, datetime] = {}
        self._threshold = failure_threshold
        self._ttl_minutes = failure_ttl_minutes
        self._lock = RLock()

    @staticmethod
    def _key(server_name: str, tool_name: str) -> str:
        return f"{server_name}:{tool_name}"

    def track_failure(self, server_name: str, tool_name: str) -> int:
        """Record a validation failure; return the current count for this tool."""
        with self._lock:
            key = self._key(server_name, tool_name)
            self._cleanup_expired()
            self._failures[key] = self._failures.get(key, 0) + 1
            self._timestamps[key] = datetime.now()
            count = self._failures[key]
            self.logger.warning(f"MCP validation failure #{count} for {key}")
            return count

    def should_inject_schema(self, server_name: str, tool_name: str) -> bool:
        """True once failures for this tool reach the threshold."""
        with self._lock:
            return self._failures.get(self._key(server_name, tool_name), 0) >= self._threshold

    def clear_failures(self, server_name: str, tool_name: str) -> None:
        """Clear the failure count after a successful execution."""
        with self._lock:
            key = self._key(server_name, tool_name)
            if key in self._failures:
                del self._failures[key]
                self._timestamps.pop(key, None)
                self.logger.debug(f"Cleared MCP validation failures for {key}")

    def reset(self) -> None:
        """Drop all tracked failures."""
        with self._lock:
            self._failures.clear()
            self._timestamps.clear()

    def _cleanup_expired(self) -> None:
        """Remove failures older than the TTL. Caller holds the lock."""
        cutoff = datetime.now() - timedelta(minutes=self._ttl_minutes)
        expired = [k for k, ts in self._timestamps.items() if ts < cutoff]
        for key in expired:
            self._failures.pop(key, None)
            self._timestamps.pop(key, None)
            self.logger.debug(f"Expired MCP validation failures for {key}")
