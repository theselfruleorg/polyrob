"""A stdio MCP server runs in its own session, so close() ends the whole group: npx and
the node grandchild it starts, and a server that ignores EOF and SIGTERM. A grandchild
that holds the pipes open also holds the reader threads and a blocking read."""
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.exceptions import MCPConnectionError
from tools.mcp import protocol, stdio_group
from tools.mcp.protocol import MCPStdioTransport


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _until_gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


async def _read_pid(path: Path) -> int:
    for _ in range(300):
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("the server did not start its grandchild")


def _server(tmp_path: Path, body: str) -> MCPStdioTransport:
    script = tmp_path / "server.py"
    script.write_text(body)
    return MCPStdioTransport(command=["python3", str(script)])


def _grandchild(tmp_path: Path, *, ignore_term: bool) -> str:
    """Server text: starts `sleep` with the inherited pipes and writes its pid."""
    flag = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else ""
    return (
        "import signal, subprocess, time\n"
        f"{flag}"
        "p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(tmp_path / 'pid')!r}, 'w').write(str(p.pid))\n"
    )


@pytest.mark.asyncio
async def test_the_server_leads_its_own_group_and_close_ends_it(tmp_path):
    t = _server(tmp_path, "import time\ntime.sleep(60)\n")
    await t.connect()
    pid = t.process.pid
    assert os.getpgid(pid) == pid
    await t.close()
    assert t.process is None
    assert await _until_gone(pid)


@pytest.mark.asyncio
async def test_close_kills_a_group_that_ignores_eof_and_sigterm(tmp_path, monkeypatch):
    monkeypatch.setattr(stdio_group, "CLOSE_WAIT_S", 0.3)
    t = _server(tmp_path, _grandchild(tmp_path, ignore_term=True) + "time.sleep(60)\n")
    await t.connect()
    leader, grandchild = t.process.pid, await _read_pid(tmp_path / "pid")
    drain = t._stderr_drain
    await t.close()
    assert await _until_gone(leader)
    assert await _until_gone(grandchild)
    drain.join(3.0)  # nobody holds the pipe, so the reader thread ends
    assert not drain._thread.is_alive()


@pytest.mark.asyncio
async def test_close_reaches_a_grandchild_after_the_leader_left(tmp_path, monkeypatch):
    """npx dies on SIGTERM and the server under it may not: the leader's exit does not
    end the wait, and the grandchild does not survive it."""
    monkeypatch.setattr(stdio_group, "CLOSE_WAIT_S", 0.3)
    body = (_grandchild(tmp_path, ignore_term=True)
            + "signal.signal(signal.SIGTERM, signal.SIG_DFL)\ntime.sleep(60)\n")
    t = _server(tmp_path, body)
    await t.connect()
    grandchild = await _read_pid(tmp_path / "pid")
    await t.close()
    assert await _until_gone(grandchild)


@pytest.mark.asyncio
async def test_close_does_not_wait_when_the_group_ends_on_sigterm(tmp_path):
    t = _server(tmp_path, _grandchild(tmp_path, ignore_term=False) + "time.sleep(60)\n")
    await t.connect()
    grandchild = await _read_pid(tmp_path / "pid")
    started = time.monotonic()
    await t.close()
    assert time.monotonic() - started < stdio_group.CLOSE_WAIT_S / 2
    assert await _until_gone(grandchild)


@pytest.mark.asyncio
async def test_a_cancelled_close_still_kills_the_group(tmp_path):
    t = _server(tmp_path, _grandchild(tmp_path, ignore_term=True) + "time.sleep(60)\n")
    await t.connect()
    grandchild = await _read_pid(tmp_path / "pid")
    closing = asyncio.create_task(t.close())
    await asyncio.sleep(0.2)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert await _until_gone(grandchild)
    assert t.process is None


@pytest.mark.asyncio
async def test_a_failed_start_reports_stderr_without_a_blocking_read(tmp_path, monkeypatch):
    """The leader exits and a grandchild holds stderr open: `stderr.read()` froze every
    session on the loop until the grandchild ended."""
    monkeypatch.setattr(protocol, "_START_CHECK_S", 1.5)
    monkeypatch.setattr(protocol, "_START_STDERR_WAIT_S", 0.2)
    body = _grandchild(tmp_path, ignore_term=False) + (
        "import sys\nprint('boom-at-start', file=sys.stderr, flush=True)\nsys.exit(1)\n")
    t = _server(tmp_path, body)
    worst, last = 0.0, time.monotonic()

    async def _beat():
        nonlocal worst, last
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            worst, last = max(worst, now - last), now

    beat = asyncio.create_task(_beat())
    try:
        with pytest.raises(MCPConnectionError, match="boom-at-start"):
            await asyncio.wait_for(t.connect(), timeout=10)
    finally:
        beat.cancel()
    assert worst < 1.0
    grandchild = await _read_pid(tmp_path / "pid")
    await t.close()
    assert await _until_gone(grandchild)


def test_a_group_left_at_exit_is_ended(tmp_path):
    """CLI Ctrl-C no longer reaches the child: what no close() ended, exit does."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import signal, time\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"],
        stdin=subprocess.PIPE, start_new_session=True)
    stdio_group.track(proc)
    time.sleep(0.3)
    stdio_group._stop_left_groups()
    assert proc.wait(timeout=3) == -signal.SIGKILL
