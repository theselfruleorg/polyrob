"""098 TS-01: an MCP ServerConnection owns one ResourceLifecycle."""
import asyncio
from types import SimpleNamespace

from tools.mcp.server_manager import MCPServerManager, ServerConnection, ServerStatus


class _Client:
    def __init__(self, log, n):
        self.transport = SimpleNamespace(is_closed=False)
        self.log, self.n = log, n

    async def close(self):
        self.log.append(("close", self.n))


class _Opener:
    """Fails ``fail`` times, then returns a fresh client; logs connect/close."""

    def __init__(self, fail=0, gate=None):
        self.fail, self.gate, self.log, self.clients = fail, gate, [], []

    async def __call__(self, conn):
        n = len(self.log)
        self.log.append(("connect", n))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            self.fail -= 1
            raise ConnectionError("down")
        c = _Client(self.log, n)
        self.clients.append(c)
        return c


def _cfg(**kw):
    return SimpleNamespace(retry_attempts=3, retry_delay=1, auto_reconnect=True, enabled=True,
                           type=SimpleNamespace(value="stdio"), **kw)


def _add(mgr, name, opener):
    c = ServerConnection(name=name, config=_cfg(), opener=opener)
    mgr.connections[name] = c
    return c


def test_error_row_is_retried_by_health_loop():
    async def go():
        mgr = MCPServerManager()
        op = _Opener(fail=2)
        c = _add(mgr, "a", op)
        assert await mgr._connect_server(c) is False
        for _ in range(3):
            c.lifecycle.next_attempt_at = 0  # backoff elapsed
            await mgr._health_check_server(c)
        assert c.status == ServerStatus.CONNECTED
    asyncio.run(go())


def test_backoff_grows_and_resets_on_ready():
    async def go():
        mgr = MCPServerManager()
        op = _Opener(fail=4)
        c = _add(mgr, "a", op)
        now = [0.0]
        c.lifecycle._clock = lambda: now[0]
        await mgr._connect_server(c)
        deltas = []
        for _ in range(4):
            deltas.append(c.lifecycle.next_attempt_at - now[0])
            now[0] = c.lifecycle.next_attempt_at
            await mgr._health_check_server(c)
        assert deltas == [1.0, 2.0, 4.0, 8.0]
        assert c.status == ServerStatus.CONNECTED and c.retry_count == 0
    asyncio.run(go())


def test_health_failure_closes_client_before_retry():
    async def go():
        mgr = MCPServerManager()
        op = _Opener()
        c = _add(mgr, "a", op)
        await mgr._connect_server(c)
        op.clients[0].transport.is_closed = True  # peer lost
        await mgr._health_check_server(c)
        assert c.status == ServerStatus.ERROR and c.client is None
        c.lifecycle.next_attempt_at = 0
        await mgr._health_check_server(c)
        assert op.log == [("connect", 0), ("close", 0), ("connect", 2)]
        assert c.status == ServerStatus.CONNECTED
    asyncio.run(go())


def test_connect_server_closes_previous_client():
    async def go():
        mgr = MCPServerManager()
        op = _Opener()
        c = _add(mgr, "a", op)
        await mgr._connect_server(c)
        old = op.clients[0]
        await c.lifecycle.fail(RuntimeError("drop"))
        assert await mgr.connect_server("a") is True
        assert op.log.count(("close", 0)) == 1 and c.client is not old
        # an explicit restart closes the live client too
        assert await mgr.restart_server("a") is True
        assert len([e for e in op.log if e[0] == "close"]) == 2
    asyncio.run(go())


def test_reconnect_aborts_for_removed_server():
    async def go():
        mgr = MCPServerManager()
        gate = asyncio.Event()
        op = _Opener(fail=1)
        c = _add(mgr, "a", op)
        await mgr._connect_server(c)
        op.gate = gate
        c.lifecycle.next_attempt_at = 0
        t = asyncio.create_task(mgr._health_check_server(c))
        await asyncio.sleep(0.01)  # reconnect is waiting inside the handshake
        rm = asyncio.create_task(mgr.remove_server("a"))
        await asyncio.sleep(0.01)
        gate.set()
        await t
        await rm
        assert "a" not in mgr.connections
        assert c.lifecycle.state == "removed" and c.client is None
        assert ("close", 1) in op.log  # the client the reconnect built was closed
        assert await mgr._connect_server(c) is False  # never revives
    asyncio.run(go())


def test_disconnect_all_closes_error_and_reconnecting_rows():
    async def go():
        mgr = MCPServerManager()
        ops = [_Opener() for _ in range(3)]
        conns = [_add(mgr, n, op) for n, op in zip("abc", ops)]
        for c in conns:
            await mgr._connect_server(c)
        ops[1].clients[0].transport.is_closed = True
        await mgr._health_check_server(conns[1])  # b: ERROR (already closed)
        await mgr.disconnect_all_servers()
        assert all(c.lifecycle.is_closed() for c in conns)
        assert ("close", 0) in ops[0].log and ("close", 0) in ops[2].log
        assert all(c.status == ServerStatus.DISCONNECTED for c in conns)
    asyncio.run(go())


def test_slow_handshake_does_not_block_other_connects():
    async def go():
        mgr = MCPServerManager()
        gate = asyncio.Event()
        a = _add(mgr, "a", _Opener(gate=gate))
        _add(mgr, "b", _Opener())
        ta = asyncio.create_task(mgr.connect_server("a"))
        await asyncio.sleep(0.01)
        assert await asyncio.wait_for(mgr.connect_server("b"), 1.0) is True
        assert a.status == ServerStatus.CONNECTING
        gate.set()
        assert await ta is True
    asyncio.run(go())


def test_add_server_limit_counts_degraded_rows():
    async def go():
        mgr = MCPServerManager(max_concurrent=1)
        c = _add(mgr, "a", _Opener(fail=1))
        await mgr._connect_server(c)
        assert c.status == ServerStatus.ERROR
        assert await mgr.add_server("b", _cfg()) is False
    asyncio.run(go())


def test_server_info_keeps_legacy_status_strings():
    async def go():
        mgr = MCPServerManager()
        c = _add(mgr, "a", _Opener())
        assert (await mgr.get_server_info("a"))["status"] == "disconnected"
        await mgr._connect_server(c)
        assert (await mgr.get_server_info("a"))["status"] == "connected"
        await mgr.disconnect_server("a")
        assert (await mgr.get_server_info("a"))["status"] == "disconnected"
        assert await mgr.connect_server("a") is True  # CLOSED is restartable
    asyncio.run(go())
