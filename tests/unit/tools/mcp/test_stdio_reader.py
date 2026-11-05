"""One long-lived reader per stdio pipe. ``wait_for(to_thread(readline))`` cancels the
future, not the thread: each 30 s idle poll left a thread blocked in ``readline``, which
filled the process-wide default executor and then ate the next reply."""
import asyncio
import io
import json
import subprocess
import sys
import threading

import pytest

from tools.mcp.protocol import MCPResponse, MCPStdioTransport
from tools.mcp.stdio_reader import LineReader, StderrDrain

_REPLY = json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"ok": True}})
_SILENT_THEN_REPLY = (
    "import sys, time\n"
    "time.sleep(1.0)\n"
    f"print({_REPLY!r}, flush=True)\n"
    "time.sleep(30)\n"
)


def _spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=0)


def test_idle_polls_keep_one_thread_and_lose_no_reply():
    async def go():
        t = MCPStdioTransport(command=["python3", "-V"], timeout=5)
        t.process = _spawn(_SILENT_THEN_REPLY)
        try:
            before = threading.active_count()
            for _ in range(8):
                assert await t.receive_message(timeout=0.02) is None
            assert threading.active_count() <= before + 1
            # The process-wide pool was never touched: it does not even exist yet.
            assert asyncio.get_running_loop()._default_executor is None
            message = await t.receive_message(timeout=5)
            assert isinstance(message, MCPResponse) and message.id == "1"
        finally:
            t.process.kill()
            t.process.wait()
            t.process = None
    asyncio.run(go())


def test_reader_applies_backpressure_when_nobody_consumes():
    async def go():
        reader = LineReader(io.StringIO("l\n" * 50), asyncio.get_running_loop(),
                            name="t", max_pending=3)
        await asyncio.sleep(0.2)
        assert reader.pending() == 3
        assert [await reader.get(1) for _ in range(5)] == ["l\n"] * 5
        reader.stop()
    asyncio.run(go())


def test_eof_is_sticky_and_ends_the_thread():
    async def go():
        reader = LineReader(io.StringIO("only\n"), asyncio.get_running_loop(), name="t")
        assert await reader.get(1) == "only\n"
        assert await reader.get(1) == ""
        assert await reader.get(1) == ""
        reader.join(1)
        assert not reader.is_alive()
    asyncio.run(go())


def test_a_timeout_raises_and_keeps_the_reader():
    async def go():
        read_end, write_end = subprocess.os.pipe()
        with subprocess.os.fdopen(read_end) as stream:
            reader = LineReader(stream, asyncio.get_running_loop(), name="t")
            with pytest.raises(asyncio.TimeoutError):
                await reader.get(0.01)
            assert reader.is_alive()
            subprocess.os.write(write_end, b"late\n")
            assert await reader.get(2) == "late\n"
            subprocess.os.close(write_end)
            reader.join(2)
    asyncio.run(go())


def test_a_read_error_reaches_the_caller():
    class Broken:
        def readline(self):
            raise ValueError("I/O operation on closed file")

    async def go():
        reader = LineReader(Broken(), asyncio.get_running_loop(), name="t")
        with pytest.raises(ValueError):
            await reader.get(1)
    asyncio.run(go())


def test_a_closed_loop_ends_the_thread():
    loop = asyncio.new_event_loop()
    release = threading.Event()

    class Slow:
        def readline(self):
            release.wait(2)
            return "line\n"

    reader = LineReader(Slow(), loop, name="t")
    loop.close()
    release.set()
    reader.join(2)
    assert not reader.is_alive()


def test_stderr_drain_keeps_a_bounded_tail():
    drain = StderrDrain(io.StringIO("".join(f"e{i}\n" for i in range(100))), max_tail=3)
    drain.join(2)
    assert drain.tail() == "e97\ne98\ne99"


def test_monitor_logs_the_tail_without_reading_the_pipe_on_the_loop():
    class NoBlockingRead(io.StringIO):
        def read(self, *a):
            raise AssertionError("a pipe a grandchild still holds would block the loop")

    async def go():
        t = MCPStdioTransport(command=["python3", "-V"], timeout=5)
        t.process = type("P", (), {"stdout": None, "stderr": NoBlockingRead("boom\n"),
                                   "poll": lambda self: 1})()
        t._stderr_drain = StderrDrain(t.process.stderr)
        t._stderr_drain.join(2)
        await asyncio.wait_for(t._monitor_process_health(), 1)
        assert t.is_peer_lost
    asyncio.run(go())
