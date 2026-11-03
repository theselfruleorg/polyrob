"""Persistent per-session Python kernel for ``run_code(persist=True)`` (073 W9+,
cross-agent ``execute_code`` parity).

Variables, imports and functions defined in one ``run_code`` call stay alive for
the next one in the same session — like a notebook. ONE long-lived interpreter per
session, driven over its stdin/stdout with one JSON line per cell:

- **where it runs** is the session's execution backend, never a new place: inside
  the session's PERSISTENT docker sandbox (``docker exec -i <container> python3``),
  or as a local child process when the backend is ``local_subprocess`` (allowed only
  where ``run_code`` itself is: ``sandbox_guard``). ssh and pack backends have no
  attached interpreter -> an honest error.
- **a timeout kills the kernel** (its state is lost and the result says so): an
  interpreter stuck in a cell cannot be trusted to answer the next one.
- **a last expression is echoed** (``repr``) like a notebook cell.
- output per cell is capped (``CODE_EXEC_MAX_OUTPUT_BYTES``) inside the kernel.

No ``@BaseTool.action`` closures here — ``from __future__`` is safe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: The interpreter loop, run with ``python -u -c``. Stdlib only.
KERNEL_SOURCE = r'''
import sys, json, io, ast, traceback, contextlib
_cap = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
_g = {"__name__": "__main__", "__builtins__": __builtins__}
_out = sys.stdout
_in = sys.stdin
import os as _os
_out.write(json.dumps({"pid": _os.getpid()}) + "\n")
_out.flush()
def _clip(s):
    return s if len(s) <= _cap else s[:_cap] + "\n...[truncated %d chars]" % (len(s) - _cap)
for _line in _in:
    try:
        _req = json.loads(_line)
    except Exception:
        continue
    _o, _e = io.StringIO(), io.StringIO()
    _ok = True
    with contextlib.redirect_stdout(_o), contextlib.redirect_stderr(_e):
        try:
            _tree = ast.parse(_req.get("code", ""), "<cell>", "exec")
            _last = None
            if _tree.body and isinstance(_tree.body[-1], ast.Expr):
                _last = ast.Expression(_tree.body.pop().value)
            exec(compile(_tree, "<cell>", "exec"), _g)
            if _last is not None:
                _v = eval(compile(_last, "<cell>", "eval"), _g)
                if _v is not None:
                    print(repr(_v))
        except SystemExit as _x:
            _ok = _x.code in (None, 0)
        except BaseException:
            _ok = False
            traceback.print_exc()
    _out.write(json.dumps({"id": _req.get("id"), "ok": _ok,
                           "stdout": _clip(_o.getvalue()), "stderr": _clip(_e.getvalue())}) + "\n")
    _out.flush()
'''


@dataclass
class CellResult:
    stdout: str = ""
    stderr: str = ""
    ok: bool = True
    timed_out: bool = False
    restarted: bool = False  # a new kernel was started for this cell
    note: str = ""


class KernelUnavailable(RuntimeError):
    """The session's backend cannot host a persistent kernel."""


class Kernel:
    """098 TS-11: the resource is the interpreter INSIDE the backend. Its argv
    is built per start from the backend's CURRENT container (a re-created
    sandbox gets a new kernel), the kernel reports its own pid, and a kill also
    kills that inner pid in the container (``docker exec`` does not forward the
    client's SIGKILL). ``execute`` and ``shutdown`` share one lock."""

    def __init__(self, backend, workdir: Optional[str]):
        self.backend = backend
        self.workdir = workdir
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.inner_pid: Optional[int] = None
        self.container: Optional[str] = None
        self.generation = None
        self._seq = 0
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return (self.proc is not None and self.proc.returncode is None
                and self.generation == getattr(self.backend, "generation", None))

    async def start(self) -> None:
        argv, env, cwd = kernel_argv(self.backend, self.workdir)
        self.container = getattr(self.backend, "_container", None)
        self.generation = getattr(self.backend, "generation", None)
        self.proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=cwd,
            start_new_session=True, limit=8 * 1024 * 1024,
        )
        self.inner_pid = None
        try:
            hello = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 30))
            self.inner_pid = int(hello["pid"])
        except Exception:
            logger.debug("kernel: no pid line", exc_info=True)

    async def _kill_inner(self) -> None:
        pid, cname = self.inner_pid, self.container
        self.inner_pid = None
        runner = getattr(self.backend, "_docker", None)
        if not pid or not cname or runner is None:
            return
        try:  # best effort, bounded: the container may already be gone
            await asyncio.wait_for(runner(["exec", cname, "kill", "-9", str(pid)], timeout=5), 6)
        except Exception:
            logger.debug("kernel: inner kill failed", exc_info=True)

    async def shutdown(self) -> None:
        async with self._lock:
            await self.kill()

    async def kill(self) -> None:
        proc, self.proc = self.proc, None
        await self._kill_inner()
        if proc is None or proc.returncode is not None:
            return
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            pass

    async def execute(self, code: str, *, timeout: float) -> CellResult:
        async with self._lock:
            restarted = False
            if not self.alive:
                await self.kill()  # a stale kernel of an old container
                await self.start()
                restarted = True
            self._seq += 1
            rid = self._seq
            assert self.proc is not None and self.proc.stdin and self.proc.stdout
            try:
                self.proc.stdin.write((json.dumps({"id": rid, "code": code}) + "\n").encode())
                await self.proc.stdin.drain()
                while True:
                    line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
                    if not line:
                        await self.kill()
                        return CellResult(ok=False, restarted=restarted,
                                          note="the kernel exited during this cell; its state is lost")
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        continue
                    if reply.get("id") == rid:
                        return CellResult(stdout=reply.get("stdout", ""), stderr=reply.get("stderr", ""),
                                          ok=bool(reply.get("ok")), restarted=restarted)
            except asyncio.TimeoutError:
                await self.kill()
                return CellResult(ok=False, timed_out=True, restarted=restarted,
                                  note=f"timed out after {timeout:g}s; the kernel was restarted "
                                       "and its variables are lost")
            except (BrokenPipeError, ConnectionResetError):
                await self.kill()
                return CellResult(ok=False, restarted=restarted,
                                  note="the kernel stopped; its state is lost")


_KERNELS: Dict[str, Kernel] = {}


def _output_cap() -> int:
    from tools.code_exec.limits import max_output_bytes
    return max(1000, max_output_bytes())


def kernel_argv(backend, workdir: Optional[str]):
    """``(argv, env, cwd)`` that start a kernel INSIDE ``backend``'s isolation.

    Raises :class:`KernelUnavailable` for a backend with no attached interpreter."""
    name = getattr(backend, "name", "") or type(backend).__name__
    cap = str(_output_cap())
    container = getattr(backend, "_container", None)
    if name == "docker" or container is not None:
        if not container or getattr(backend, "_session_id", None) is None:
            raise KernelUnavailable(
                "persist=True needs the session's PERSISTENT docker sandbox "
                "(AGENT_COMPUTE_POSTURE>=1); this run uses a one-shot container")
        from tools.code_exec.backends.docker import docker_binary
        from tools.code_exec.env_policy import build_child_env
        argv = [docker_binary(), "exec", "-i", "-w", "/workspace"]
        if getattr(backend, "user", None):
            argv += ["--user", str(backend.user)]
        if getattr(backend, "_dev_mode", False):  # WS-1: /install stays importable
            argv += ["-e", "PYTHONPATH=/install", "-e", "HOME=/install"]
        argv += [container, "python3", "-u", "-s", "-c", KERNEL_SOURCE, cap]
        return argv, build_child_env({}), None
    if name in ("local_subprocess", "local"):
        from tools.code_exec.env_policy import build_child_env
        return [sys.executable, "-u", "-I", "-c", KERNEL_SOURCE, cap], build_child_env({}), workdir
    raise KernelUnavailable(f"persist=True is not available on the '{name}' backend")


async def run_cell(session_id: str, backend, code: str, *, timeout: float,
                   workdir: Optional[str], reset: bool = False) -> CellResult:
    sid = session_id or "kernel"
    k = _KERNELS.get(sid)
    if reset and k is not None:
        await k.shutdown()
        _KERNELS.pop(sid, None)
        k = None
    if k is None:
        kernel_argv(backend, workdir)  # raises KernelUnavailable before registering
        k = Kernel(backend, workdir)
        _KERNELS[sid] = k
    return await k.execute(code, timeout=timeout)


async def shutdown_kernel(session_id: str) -> None:
    k = _KERNELS.pop(session_id or "kernel", None)
    if k is not None:
        await k.shutdown()


__all__ = ["run_cell", "shutdown_kernel", "kernel_argv", "KernelUnavailable", "CellResult",
           "KERNEL_SOURCE"]
