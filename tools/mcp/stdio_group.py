"""A stdio MCP server's whole process group: stop it, and stop what no close() reached.

The server starts with ``start_new_session=True``, so ``npx`` and the node process under
it share one group that a signal reaches at once. The terminal's Ctrl-C no longer reaches
that group: ``stop_group`` must run, the exit hook covers a transport nobody closed, and
the EOF on stdin when this process dies is the last fallback.
"""
import asyncio
import atexit
import os
import signal
import subprocess
import time
import weakref

#: Seconds the group gets to end on SIGTERM before SIGKILL.
CLOSE_WAIT_S = 5.0
_EXIT_WAIT_S = 1.0
_REAP_WAIT_S = 5.0
_POLL_S = 0.05
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

_live: "weakref.WeakSet[subprocess.Popen]" = weakref.WeakSet()


def track(proc: subprocess.Popen) -> None:
    """Hold ``proc`` for the exit hook until ``stop_group`` takes it."""
    _live.add(proc)


def _pid_in_use(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(proc: subprocess.Popen, sig: int) -> bool:
    """Send ``sig`` to the group. False when nothing is left to signal."""
    if not hasattr(os, "killpg"):  # no process groups on this OS: the leader only
        if proc.poll() is not None:
            return False
        if sig:
            proc.send_signal(sig)
        return True
    if proc.returncode is not None and _pid_in_use(proc.pid):
        return False  # the leader was reaped, so a process with its pid is a stranger
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM when only zombies remain
        return False
    return True


def _group_alive(proc: subprocess.Popen) -> bool:
    proc.poll()  # reap the leader: a zombie still counts as a member
    return _signal_group(proc, 0)


async def stop_group(proc: subprocess.Popen) -> None:
    """EOF on stdin, SIGTERM to the group, wait, SIGKILL what is left, reap the leader.

    The wait is on the GROUP: when the leader exits on SIGTERM and a grandchild does not,
    that grandchild still holds the pipes and the reader threads. A cancelled close kills
    at once and leaves the reap to ``Popen``.
    """
    _live.discard(proc)
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except (OSError, ValueError):  # the child already closed its end
            pass
    gone = not _signal_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + CLOSE_WAIT_S
    try:
        while not gone and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_S)
            gone = not _group_alive(proc)
    finally:
        if not gone:
            _signal_group(proc, _SIGKILL)
    if proc.poll() is None:
        try:
            await asyncio.to_thread(proc.wait, _REAP_WAIT_S)
        except subprocess.TimeoutExpired:
            pass


def _stop_left_groups() -> None:
    """The exit hook: SIGTERM, a short wait, SIGKILL. Never raises into the shutdown."""
    procs = list(_live)
    _live.clear()
    try:
        for proc in procs:
            _signal_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + _EXIT_WAIT_S
        while time.monotonic() < deadline and any(_group_alive(p) for p in procs):
            time.sleep(_POLL_S)
        for proc in procs:
            _signal_group(proc, _SIGKILL)
            proc.poll()
    except Exception:  # noqa: BLE001 - the interpreter is exiting
        pass


atexit.register(_stop_left_groups)
