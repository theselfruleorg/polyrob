"""098 CL-31: one MCP transport drop must not kill the server until restart."""
import asyncio
from types import SimpleNamespace

import pytest

from tools.mcp.server_manager import MCPServerManager, ServerConnection, ServerStatus
from utils.circuit_breaker import CircuitBreaker, CircuitState


class _Client:
    def __init__(self):
        self.transport = SimpleNamespace(is_closed=False)
        self.closed = 0

    async def close(self):
        self.closed += 1


def _conn(results):
    cfg = SimpleNamespace(retry_attempts=3, retry_delay=0, auto_reconnect=True)
    c = ServerConnection(name="gw", config=cfg, circuit_breaker=CircuitBreaker(name="mcp_server_gw"))

    async def opener(_conn):
        if not next(results):
            raise ConnectionError("down")
        return _Client()
    c.opener = opener
    return c


def test_reconnect_resets_breaker_without_typeerror():
    async def go():
        mgr = MCPServerManager()
        c = _conn(iter([False, True]))
        mgr.connections["gw"] = c
        assert await mgr._connect_server(c) is False
        assert c.status == ServerStatus.ERROR
        c.circuit_breaker.state = CircuitState.OPEN
        c.lifecycle.next_attempt_at = 0
        assert await mgr._attempt_reconnection(c) is True
        assert c.status == ServerStatus.CONNECTED
        assert c.circuit_breaker.state == CircuitState.CLOSED
    asyncio.run(go())


def test_failed_reconnect_leaves_error_and_health_loop_retries():
    async def go():
        mgr = MCPServerManager()
        c = _conn(iter([False, False, True]))
        mgr.connections["gw"] = c
        await mgr._connect_server(c)
        for _ in range(2):
            c.lifecycle.next_attempt_at = 0
            await mgr._health_check_server(c)
        assert c.status == ServerStatus.CONNECTED
    asyncio.run(go())


@pytest.mark.parametrize("name", ["get_circuit_breaker_status", "reset_circuit_breaker",
                                  "reset_all_circuit_breakers"])
def test_dead_breaker_methods_removed(name):
    assert not hasattr(MCPServerManager, name)
