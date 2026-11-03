"""One MCP server connection = one ResourceLifecycle (098 TS-01).

The ``MCPClient`` is the resource. The lifecycle owns the handle, the lock and
the retry backoff; ``ServerConnection.status`` is a VIEW of the lifecycle state
(the legacy ``ServerStatus`` strings API callers read), never a second copy.
No code assigns ``client`` outside the lifecycle, so no path replaces a live
client (and its stdio child) without closing it.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.state.lifecycle import (
    CLOSED, CLOSING, DEGRADED, NEW, READY, REMOVED, STARTING, ResourceLifecycle,
)

#: The longest wait between two reconnect tries (seconds).
RECONNECT_BACKOFF_CAP = 300.0


class ServerStatus(str, Enum):
    """MCP server connection status (a view of the lifecycle)."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    RECONNECTING = "reconnecting"


_VIEW = {
    NEW: ServerStatus.DISCONNECTED,
    STARTING: ServerStatus.CONNECTING,
    READY: ServerStatus.CONNECTED,
    DEGRADED: ServerStatus.ERROR,
    CLOSING: ServerStatus.DISCONNECTED,
    CLOSED: ServerStatus.DISCONNECTED,
    REMOVED: ServerStatus.DISCONNECTED,
}

#: States that hold (or are about to hold) a live client — counted by limits.
ACTIVE_STATES = frozenset({STARTING, READY, DEGRADED})


class ServerConnection:
    """A connection to one MCP server.

    ``opener`` (bound by the manager) builds and handshakes a client and returns
    it; a raise is a failed start (the opener closes its own partial client).
    """

    def __init__(self, name: str, config: Any, *, status: Optional[ServerStatus] = None,
                 client: Any = None, circuit_breaker: Any = None,
                 last_error: Optional[str] = None,
                 opener: Optional[Callable[["ServerConnection"], Awaitable[Any]]] = None):
        del status  # legacy constructor argument: the status is the lifecycle view
        self.name = name
        self.config = config
        self.circuit_breaker = circuit_breaker
        self.opener = opener
        self._last_error = last_error
        self.connected_at: Optional[float] = None
        self.tools: List[Any] = []
        self.resources: List[Any] = []
        self.capabilities: Dict[str, Any] = {}
        delay = float(getattr(config, "retry_delay", 0) or 0)
        self.lifecycle: ResourceLifecycle = ResourceLifecycle(
            f"mcp:{name}", create=self._open, teardown=self._close,
            probe=_transport_alive, backoff_base=max(delay, 1.0),
            backoff_cap=RECONNECT_BACKOFF_CAP)
        if client is not None:
            self.lifecycle.adopt(client)
            self.connected_at = time.time()

    # -- views ---------------------------------------------------------------

    @property
    def status(self) -> ServerStatus:
        if self.lifecycle.state == STARTING and self.lifecycle.attempts:
            return ServerStatus.RECONNECTING
        return self.lifecycle.view(_VIEW, ServerStatus.DISCONNECTED)

    @property
    def client(self) -> Any:
        return self.lifecycle.handle

    @property
    def retry_count(self) -> int:
        return self.lifecycle.attempts

    @property
    def last_error(self) -> Optional[str]:
        err = self.lifecycle.last_error
        return str(err) if err is not None else self._last_error

    @last_error.setter
    def last_error(self, value: Optional[str]) -> None:
        self._last_error = value

    def is_active(self) -> bool:
        return self.lifecycle.state in ACTIVE_STATES

    # -- lifecycle hooks -----------------------------------------------------

    async def _open(self) -> Any:
        if self.opener is None:
            raise RuntimeError(f"MCP connection {self.name!r} is not bound to a manager")
        client = await self.opener(self)
        self.connected_at = time.time()
        self._last_error = None
        return client

    async def _close(self, client: Any) -> None:
        self.connected_at = None
        self.tools = []
        self.resources = []
        self.capabilities = {}
        await client.close()


def _transport_alive(client: Any) -> bool:
    transport = getattr(client, "transport", None)
    return client is not None and not getattr(transport, "is_closed", True)
