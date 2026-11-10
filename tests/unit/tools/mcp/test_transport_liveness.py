"""098 TS-02: MCP transport OPEN -> PEER_LOST -> CLOSED."""
import asyncio
import io
import time
from types import SimpleNamespace

import pytest

from core.exceptions import MCPConnectionError, MCPProtocolError
from tools.mcp.protocol import MCPClient, MCPSSETransport, MCPStdioTransport
from tools.mcp.transport_state import TRANSPORT


def _stdio_with_stdout(data=""):
    t = MCPStdioTransport(command=["python3", "-V"], timeout=5)
    t.process = SimpleNamespace(stdout=io.StringIO(data), stderr=None, poll=lambda: None)
    return t


def test_table_is_well_formed():
    assert TRANSPORT.validate() == []


def test_stdio_eof_marks_peer_lost():
    async def go():
        t = _stdio_with_stdout("")
        with pytest.raises(MCPConnectionError):
            await t.receive_message(timeout=1)
        assert t.is_closed and t.is_peer_lost
        t.process = None
        await t.close()
        assert t.is_closed and not t.is_peer_lost
    asyncio.run(go())


def test_monitor_exit_marks_peer_lost():
    async def go():
        t = MCPStdioTransport(command=["python3", "-V"], timeout=5)
        t.process = SimpleNamespace(stdout=None, stderr=None, poll=lambda: 1)
        await asyncio.wait_for(t._monitor_process_health(), 1)
        assert t.is_peer_lost
    asyncio.run(go())


def test_sse_stream_end_marks_peer_lost():
    async def go():
        t = MCPSSETransport(url="http://example.invalid/sse", timeout=5)

        async def _lines():
            for x in (b"data: {\"jsonrpc\":\"2.0\",\"id\":\"1\",\"result\":{}}\n",):
                yield x
        t._response = SimpleNamespace(content=_lines())
        await t._read_sse_messages()
        assert t.is_closed and t.is_peer_lost
        # a queued message is still delivered, then the loss is raised
        assert (await t.receive_message(timeout=1)).id == "1"
        with pytest.raises(MCPConnectionError):
            await t.receive_message(timeout=1)
    asyncio.run(go())


def _client_on(transport):
    c = MCPClient(transport)
    c._connected = True
    return c


def test_process_messages_exits_on_peer_lost_and_fails_pending():
    async def go():
        t = _stdio_with_stdout("")
        c = _client_on(t)
        fut = asyncio.get_running_loop().create_future()
        c._pending_requests["r1"] = fut
        await asyncio.wait_for(c.process_messages(), 1)
        with pytest.raises(MCPConnectionError):
            await fut
        assert not c._message_loop_alive.is_set()
    asyncio.run(go())


def test_no_busy_loop_after_eof():
    async def go():
        t = _stdio_with_stdout("")
        calls = []
        orig = t.receive_message

        async def counting(timeout=None):
            calls.append(1)
            return await orig(timeout=timeout)
        t.receive_message = counting
        c = _client_on(t)
        task = asyncio.create_task(c.process_messages())
        await asyncio.sleep(0.2)
        task.cancel()
        assert len(calls) <= 2
    asyncio.run(go())


def test_stale_sweeper_does_not_cancel_caller(monkeypatch):
    async def go():
        t = _stdio_with_stdout("")
        c = _client_on(t)
        fut = asyncio.get_running_loop().create_future()
        fut._timeout_at = time.time() - 1
        fut._request_method = "tools/call"
        c._pending_requests["abcdefgh"] = fut
        real_sleep = asyncio.sleep

        async def fast_sleep(_s):
            c._connected = False  # one sweep only
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)
        await c._cleanup_pending_requests()
        monkeypatch.setattr(asyncio, "sleep", real_sleep)

        async def caller():
            return await fut
        with pytest.raises(MCPProtocolError):
            await asyncio.create_task(caller())
    asyncio.run(go())


def test_close_fails_pending_with_error_not_cancel():
    async def go():
        t = _stdio_with_stdout("")
        t.process = None
        c = _client_on(t)
        fut = asyncio.get_running_loop().create_future()
        c._pending_requests["r"] = fut
        await c.close()
        assert not fut.cancelled()
        with pytest.raises(MCPConnectionError):
            fut.result()
    asyncio.run(go())


class _BrokenPipe(io.StringIO):
    def readline(self, *a):
        raise OSError("broken pipe")


def test_stdio_read_error_marks_peer_lost_not_silence():
    """A pipe error ends the reader thread: the transport is lost, never an idle
    'no message' on every later call."""
    async def go():
        t = _stdio_with_stdout()
        t.process.stdout = _BrokenPipe()
        with pytest.raises(MCPConnectionError):
            await t.receive_message(timeout=1)
        assert t.is_peer_lost
        with pytest.raises(MCPConnectionError):
            await t.receive_message(timeout=0.05)
    asyncio.run(go())
