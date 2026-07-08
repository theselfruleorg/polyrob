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

Host-tier (posture 3) execution would be a second executor; deferred here.
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
_DEFAULT_LOG_CAP = 100_000  # bytes of a job log we ever return in one read


class DockerShellExecutor:
    """Runs shell commands inside one session's persistent DockerBackend container."""

    def __init__(self, backend):
        self._backend = backend

    async def run_foreground(
        self, command: str, state: ShellState, *, timeout: float
    ) -> Tuple[str, ShellState, int]:
        """Run ``command`` with persisted cwd/env; return (clean_output, new_state, rc)."""
        script = wrap_command(command, state)
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

    async def start_background(self, command: str, job_id: str, state: ShellState) -> None:
        """Launch ``command`` detached; pid+log persisted, survives the call.

        Robustness notes (learned against real docker — a naive ``setsid cmd & echo
        $!`` reported the job dead a second later: the exec launcher exiting reaped the
        backgrounded child, and ``$!`` captured a forked setsid that had already
        exited):
        - the command is written to a per-job ``.cmd`` file (in writable /workspace)
          so ANY command runs verbatim with zero shell-quoting hazard;
        - the server becomes the ``docker exec -d`` MAIN process via ``exec`` — docker
          keeps a ``-d`` process alive until it exits, so there is no backgrounding
          race and no launcher to reap it;
        - the launcher records its own pid via ``$$`` then ``exec``s the command, so
          the saved pid is the live process;
        - stdin is ``/dev/null`` so a server never blocks waiting on input.
        """
        job_id = _safe_job_id(job_id)
        pid_f = f"{_JOBS_DIR}/{job_id}.pid"
        log_f = f"{_JOBS_DIR}/{job_id}.log"
        cmd_f = f"{_JOBS_DIR}/{job_id}.cmd"
        q_cwd = shlex.quote(state.cwd)
        exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in state.env.items())
        launch = (
            f"mkdir -p {_JOBS_DIR}\n"
            f"printf '%s' {shlex.quote(command)} > {cmd_f}\n"
            f"cd {q_cwd} 2>/dev/null || cd /workspace\n"
            f"{exports}"
            f"echo $$ > {pid_f}\n"
            f"exec sh {cmd_f} > {log_f} 2>&1 < /dev/null\n"
        )
        await self._backend.exec_detached(launch)

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
        cap = max(1, min(int(max_bytes), _DEFAULT_LOG_CAP))
        code = f"tail -c {cap} {_JOBS_DIR}/{job_id}.log 2>/dev/null || cat {_JOBS_DIR}/{job_id}.log 2>/dev/null"
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        return result.stdout or ""

    async def kill(self, job_id: str) -> bool:
        """Best-effort tree-kill of a background job, then SIGKILL.

        The recorded pid is the launcher (``echo $$; exec sh cmdfile``), which the
        docker-exec runs as a session/pgroup leader on the common runtimes — so
        ``kill -<pid>`` (negative == process group) reaps the whole group. As a
        belt-and-suspenders for a runtime where the pid is NOT a group leader (so the
        pgroup kill ESRCHes), also ``pkill -P`` its direct children and fall back to
        the single pid — a multi-process server (workers) shouldn't orphan children.
        """
        job_id = _safe_job_id(job_id)
        code = (
            f"pid=$(cat {_JOBS_DIR}/{job_id}.pid 2>/dev/null); "
            f"if [ -n \"$pid\" ]; then "
            f"kill -TERM -\"$pid\" 2>/dev/null; pkill -TERM -P \"$pid\" 2>/dev/null; "
            f"kill -TERM \"$pid\" 2>/dev/null; "
            f"sleep 0.2; "
            f"kill -KILL -\"$pid\" 2>/dev/null; pkill -KILL -P \"$pid\" 2>/dev/null; "
            f"kill -KILL \"$pid\" 2>/dev/null; "
            f"echo KILLED; else echo NOPID; fi"
        )
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=code, timeout=10, dev_mode=True,
        ))
        return "KILLED" in (result.stdout or "")
