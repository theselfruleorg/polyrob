"""MCP resource subscription callback registry (Item 7F).

Tracks ``(server, uri) -> [callbacks]`` so a server-side
``notifications/resources/updated`` can be routed to interested listeners. The
``MCPServerManager`` owns one registry; ``subscribe_resource`` registers a callback
(default: invalidate cache + emit telemetry) and ``handle_resource_updated`` (driven
by the client's notification loop) dispatches to them.

Callbacks may be sync or async. Dispatch is fail-open per callback — one raising
listener never suppresses the rest.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

Key = Tuple[str, str]


class ResourceSubscriptionRegistry:
    """In-memory ``(server, uri) -> callbacks`` registry with async dispatch."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self.logger = log or logger
        self._subs: Dict[Key, List[Callable]] = {}

    def subscribe(self, server: str, uri: str, callback: Callable) -> None:
        self._subs.setdefault((server, uri), []).append(callback)

    def unsubscribe(self, server: str, uri: str, callback: Callable | None = None) -> None:
        """Remove ``callback`` for the key, or ALL callbacks for the key if None."""
        key = (server, uri)
        if callback is None:
            self._subs.pop(key, None)
            return
        cbs = self._subs.get(key)
        if not cbs:
            return
        self._subs[key] = [c for c in cbs if c is not callback]
        if not self._subs[key]:
            self._subs.pop(key, None)

    def is_subscribed(self, server: str, uri: str) -> bool:
        return bool(self._subs.get((server, uri)))

    def uris_for(self, server: str) -> List[str]:
        """Every resource uri currently subscribed on ``server`` (H10: used to
        re-establish subscriptions after a reconnect)."""
        return [uri for (srv, uri) in self._subs if srv == server]

    def clear(self, server: str) -> None:
        """Drop every subscription for ``server`` (e.g. on disconnect)."""
        for key in [k for k in self._subs if k[0] == server]:
            self._subs.pop(key, None)

    async def dispatch(self, server: str, uri: str, **payload: Any) -> int:
        """Fire all callbacks for ``(server, uri)``. Returns how many were attempted."""
        cbs = list(self._subs.get((server, uri), []))
        for cb in cbs:
            try:
                result = cb(server, uri, **payload) if payload else cb(server, uri)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # fail-open: one bad listener must not break others
                self.logger.error(
                    f"resource.callback.error server={server} uri={uri} "
                    f"exc={type(e).__name__}: {e}"
                )
        return len(cbs)
