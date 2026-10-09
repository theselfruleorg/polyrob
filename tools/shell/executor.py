"""Shell executor — runs shell commands in the session's persistent sandbox (WS-2/3).

The `DockerShellExecutor` drives a persistent `DockerBackend` container:
- **foreground** reuses the backend's hardened `run()` (docker exec + in-container
  `timeout`), wrapping the command via `state.wrap_command` and parsing the trailing
  cwd/env block back out;
- **background** uses a detached exec (`docker exec -d`) so a launched server survives
  the call, writing pid+log under `/tmp/polyrob-jobs/<id>.*`;
- **poll/log/kill** are short foreground bash one-liners against the same container
  (poll = `kill -0` the saved pid; kill = signal the pid's process group, which a
  `setsid`-launched job heads, for a tree-kill).

The host tier (posture 3) is the second executor, ``tools/shell/host_executor.py``
(073 W1); ``tools/shell/backend_pool.py::resolve_shell_executor`` picks one per call.
Both expose the same surface: ``kind``, ``default_cwd``, ``run_foreground``,
``start_background``, ``poll``, ``read_log``, ``read_log_from``, ``exit_code``,
``kill``, ``write_stdin``, ``close_stdin`` (073 W4: stdin is a FIFO per job —
``tools/shell/jobs.py``).
"""
from __future__ import annotations

import logging
import re
import shlex
from typing import Tuple

from tools.code_exec.result import ExecutionRequest
from tools.shell.state import ShellState, wrap_command, parse_state

logger = logging.getLogger(__name__)

#: Job ids are minted as ``job-<counter>`` by the registry; validate defensively
#: before interpolating into any shell one-liner (poll/log/kill), so a future caller
#: that skips the registry membership-check can never turn this into shell injection.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_job_id(job_id: str) -> str:
    if not job_id or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return job_id

# Background-job control files (pid/log/cmd) live under /tmp, NOT /workspace:
# the hardened sandbox forces a non-root container user (65534:65534 when the host
# process is root, as on the prod server), and the bind-mounted /workspace is
# root-owned → a non-root `mkdir /workspace/.jobs` fails with EACCES (live prod
# 2026-07-07). /tmp is a world-writable tmpfs every container user can write. It is
# per-container, but poll/log/kill run in the SAME session container (backend_pool),
# so they see the same files; job state dying with the container is fine (the jobs
# die with it too).
_JOBS_DIR = "/tmp/polyrob-jobs"
_DEFAULT_LOG_CAP = 100_000  # bytes of a job log a read returns by default
#: Hard ceiling for one log read (a durable receipt keeps up to 200 000 chars).
_MAX_LOG_CAP = 200_000


class DockerShellExecutor:
    """Runs shell commands inside one session's persistent DockerBackend container."""

    kind = "docker"
    default_cwd = "/workspace"

    def __init__(self, backend):
        self._backend = backend

    async def run_foreground(
        self, command: str, state: ShellState, *, timeout: float, workdir: str = ""
    ) -> Tuple[str, ShellState, int]:
        """Run ``command`` with persisted cwd/env; return (clean_output, new_state, rc)."""
        script = wrap_command(command, state, workdir=workdir)
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=script, timeout=timeout, dev_mode=True,
        ))
        # parse_state operates on stdout only (the state block rides stdout); keep
        # any stderr appended to the clean text the model sees.
        clean, new_state = parse_state(result.stdout or "", state)
        if result.stderr:
            clean = f"{clean}\n[stderr]\n{result.stderr}" if clean else f"[stderr]\n{result.stderr}"
        rc = result.exit_code if result.exit_code is not None else 0
        return clean, new_state, rc

    async def start_background(self, command: str, job_id: str, state: ShellState, *,
                               workdir: str = "", pty: bool = False) -> None:
        """Launch ``command`` detached; pid+log+rc persisted, survives the call.

        Robustness notes (learned against real docker — a naive ``setsid cmd & echo
        $!`` reported the job dead a second later: the exec launcher exiting reaped the
        backgrounded child, and ``$!`` captured a forked setsid that had already
        exited):
        - the command is written to a per-job ``.cmd`` file so ANY command runs
          verbatim with zero shell-quoting hazard;
        - the launcher is the ``docker exec -d`` MAIN process and WAITS for the job
          (073 W4: it writes the exit code to ``.rc`` and stops the stdin keeper) —
          docker keeps a ``-d`` process alive until it exits, so there is no
          backgrounding race and no launcher exit to reap the job;
        - the saved pid is the launcher's (``$$``), which heads the job's group;
        - stdin is the job's FIFO held open by a keeper (``tools/shell/jobs.py``),
          so the job never blocks on the open and ``process write`` reaches it.
        """
        if pty:
            raise ValueError("pty=True runs only on the host executor (posture 3), "
                             "not in the sandbox container")
        from tools.shell.jobs import command_file_text, job_files, launcher_script
        job_id = _safe_job_id(job_id)
        files = job_files(_JOBS_DIR, job_id)
        q_cwd = shlex.quote(state.cwd)
        exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in state.env.items())
        pre = (
            f"mkdir -p {_JOBS_DIR}\n"
            f"printf '%s' {shlex.quote(command_file_text(command, workdir))} > {files['cmd']}\n"
            f"cd {q_cwd} 2>/dev/null || cd /workspace\n"
        )
        await self._backend.exec_detached(
            launcher_script(shell="sh", files=files, exports=exports, pre=pre))

    async def poll(self, job_id: str) -> str:
        """Return 'running' | 'done' | 'unknown' for a background job."""
        job_id = _safe_job_id(job_id)
        code = (
            f"pid=$(cat {_JOBS_DIR}/{job_id}.pid 2>/dev/null); "
            f"if [ -z \"$pid\" ]; then echo UNKNOWN; "
            f"elif kill -0 \"$pid\" 2>/dev/null; then echo RUNNING; "
            f"else echo DONE; fi"
        )
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        out = (result.stdout or "").strip().upper()
        if out.startswith("RUNNING"):
            return "running"
        if out.startswith("DONE"):
            return "done"
        return "unknown"

    async def read_log(self, job_id: str, *, max_bytes: int = _DEFAULT_LOG_CAP) -> str:
        """Return the tail (up to ``max_bytes``) of a background job's log."""
        job_id = _safe_job_id(job_id)
        cap = max(1, min(int(max_bytes), _MAX_LOG_CAP))
        code = f"tail -c {cap} {_JOBS_DIR}/{job_id}.log 2>/dev/null || cat {_JOBS_DIR}/{job_id}.log 2>/dev/null"
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        return result.stdout or ""

    async def kill(self, job_id: str) -> bool:
        """Best-effort tree-kill of a background job, then SIGKILL.

        The recorded pid is the launcher (``echo $$``; it waits for the job), which the
        docker-exec runs as a session/pgroup leader on the common runtimes — so
        ``kill -<pid>`` (negative == process group) reaps the whole group. As a
        belt-and-suspenders for a runtime where the pid is NOT a group leader (so the
        pgroup kill ESRCHes), also ``pkill -P`` its direct children (the job shell
        and the stdin keeper) and the job shell's children (``.jpid``), and fall
        back to the single pid — a multi-process server shouldn't orphan children.
        """
        job_id = _safe_job_id(job_id)
        code = (
            f"pid=$(cat {_JOBS_DIR}/{job_id}.pid 2>/dev/null); "
            f"if [ -n \"$pid\" ]; then "
            f"j=$(cat {_JOBS_DIR}/{job_id}.jpid 2>/dev/null); "
            f"kill -TERM -\"$pid\" 2>/dev/null; pkill -TERM -P \"$pid\" 2>/dev/null; "
            f"[ -n \"$j\" ] && pkill -TERM -P \"$j\" 2>/dev/null; "
            f"kill -TERM \"$pid\" 2>/dev/null; "
            f"sleep 0.2; "
            f"kill -KILL -\"$pid\" 2>/dev/null; pkill -KILL -P \"$pid\" 2>/dev/null; "
            f"[ -n \"$j\" ] && pkill -KILL -P \"$j\" 2>/dev/null; "
            f"kill -KILL \"$pid\" 2>/dev/null; "
            f"echo KILLED; else echo NOPID; fi"
        )
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        return "KILLED" in (result.stdout or "")

    async def exit_code(self, job_id: str):
        """The exit code the launcher recorded in ``.rc`` (073 W4); None = unknown
        (still running, killed, or a job launched before the launcher wrote one)."""
        job_id = _safe_job_id(job_id)
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=f"cat {_JOBS_DIR}/{job_id}.rc 2>/dev/null",
            timeout=10, dev_mode=True,
        ))
        try:
            return int((result.stdout or "").strip())
        except ValueError:
            return None

    async def read_log_from(self, job_id: str, offset: int, *,
                            max_bytes: int = 64 * 1024) -> Tuple[str, int]:
        """New log text since byte ``offset`` -> ``(text, new_offset)`` (073 W4 notify)."""
        job_id = _safe_job_id(job_id)
        off = max(0, int(offset))
        cap = max(1, int(max_bytes))
        log_f = f"{_JOBS_DIR}/{job_id}.log"
        code = (
            f"sz=$(wc -c < {log_f} 2>/dev/null || echo 0); sz=$(echo $sz); echo \"$sz\"; "
            f"if [ \"$sz\" -gt {off} ]; then tail -c +{off + 1} {log_f} | head -c {cap}; fi"
        )
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        out = result.stdout or ""
        head, _, text = out.partition("\n")
        try:
            size = int(head.strip())
        except ValueError:
            return "", off
        if size < off:          # the log was replaced: start over
            return "", 0
        return text, min(size, off + cap)

    async def write_stdin(self, job_id: str, data: bytes) -> Tuple[bool, str]:
        """Send ``data`` to the job's stdin FIFO (073 W4). ``(ok, message)``.

        Base64 on the command line (no quoting hazard, bounded size); ``timeout``
        bounds the FIFO open, which blocks when no reader is left."""
        import base64
        from tools.shell.jobs import STDIN_MAX_BYTES, STDIN_WRITE_TIMEOUT_SEC, job_files
        job_id = _safe_job_id(job_id)
        if len(data) > STDIN_MAX_BYTES:
            return False, f"stdin write is limited to {STDIN_MAX_BYTES} bytes"
        f = job_files(_JOBS_DIR, job_id)
        b64 = base64.b64encode(data).decode("ascii")
        t = int(STDIN_WRITE_TIMEOUT_SEC)
        code = (
            f"if [ -e {f['closed']} ]; then echo CLOSED; "
            f"elif [ ! -p {f['in']} ]; then echo NOFIFO; "
            f"elif [ -e {f['rc']} ]; then echo NOREADER; "
            f"elif printf '%s' '{b64}' | base64 -d | timeout {t} sh -c 'cat > \"$1\"' _ {f['in']}; "
            f"then echo WROTE; else echo NOREADER; fi"
        )
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=t + 5, dev_mode=True,
        ))
        out = (result.stdout or "").strip()
        if out.endswith("WROTE"):
            return True, f"wrote {len(data)} bytes"
        if out.endswith("CLOSED"):
            return False, "stdin of this job is closed (process close was called)"
        if out.endswith("NOFIFO"):
            return False, "this job has no stdin (it was started without one)"
        return False, "the job is not reading its stdin (it may have exited)"

    async def close_stdin(self, job_id: str) -> Tuple[bool, str]:
        """EOF on the job's stdin: stop the keeper, mark the FIFO closed (073 W4)."""
        from tools.shell.jobs import job_files
        job_id = _safe_job_id(job_id)
        f = job_files(_JOBS_DIR, job_id)
        code = (
            f"if [ -e {f['closed']} ]; then echo ALREADY; "
            f"elif [ ! -p {f['in']} ]; then echo NOFIFO; "
            f"else touch {f['closed']}; k=$(cat {f['keeper']} 2>/dev/null); "
            f"[ -n \"$k\" ] && kill \"$k\" 2>/dev/null; echo CLOSED; fi"
        )
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        out = (result.stdout or "").strip()
        if out.endswith("CLOSED"):
            return True, "stdin closed (EOF sent)"
        if out.endswith("ALREADY"):
            return True, "stdin was already closed"
        return False, "this job has no stdin (it was started without one)"
