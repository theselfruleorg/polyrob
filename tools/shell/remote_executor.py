"""Remote shell executor — ssh and every pack execution backend (073 W6/W7).

ONE executor over a small transport seam, so the persistent-shell semantics
(snapshot-replay cwd/env, background jobs with pid/log/rc files, tree kill) are
written once for every remote place a command can run:

- :class:`SshTransport` — the system ``ssh`` client with a persistent
  ControlMaster session (``ControlMaster=auto``, ``ControlPath=<session dir>/ssh-%C``,
  ``ControlPersist=600``): one TCP+auth handshake per session, not per command.
  The argv and its option-injection guards are ``SshBackend.ssh_argv``'s; process
  control is the shared ``run_group`` (own process group, bounded capture).
- :class:`BackendTransport` — any ``ExecutionBackend`` that runs ``bash``
  (Modal, Daytona, Vercel Sandbox, Singularity — the W7 packs). ``exec_detached``
  is used for the job launcher when the backend has it.

State rides IN-BAND: ``state.wrap_command`` appends the sentinel-framed
``pwd``/``env`` block to stdout and ``state.parse_state`` reads it back — no temp
file on the far side. Background jobs: the shared launcher of
``tools/shell/jobs.py`` (pid / log / rc / the stdin FIFO + keeper / jpid — the
same files the container and the host use, so ``process`` write/close/wait and
notify work alike) detached with ``nohup setsid`` (``setsid`` when the far side
has it) under a per-session remote job dir; the launcher is the session leader,
so a kill reaps the whole job tree. ``pty`` is host-only and refused here.

The executor is never a fallback for anything: ``backend_pool`` builds one only
for an explicit ``SHELL_BACKEND=ssh|<pack backend>``, and on a server only when the
transport advertises ``capabilities["sandbox"] is True``.

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shlex
import shutil
import stat
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Protocol, Set, Tuple

from tools.code_exec.backends._proc import run_group
from tools.shell.executor import _DEFAULT_LOG_CAP, _safe_job_id
from tools.shell.state import ShellState, parse_state, wrap_command

logger = logging.getLogger(__name__)

#: coreutils ``timeout`` exit codes (124 = it fired; 137 = 128+SIGKILL).
_TIMEOUT_EXIT_CODES = frozenset({124, 137})

#: Unix-socket paths are capped at 104 (macOS) / 108 (Linux) bytes. ``ssh-%C``
#: expands to 44 characters, so a longer control dir moves to a short private one.
_MAX_CONTROL_DIR_LEN = 56

#: (argv, *, stdin_bytes, timeout, label) -> (rc, stdout, stderr, timed_out) — the
#: ``run_group`` shape; injectable so the tests need no ssh binary or network.
ProcRunner = Callable[..., Awaitable[Tuple[int, str, str, bool]]]


class RemoteShellError(RuntimeError):
    """The remote side could not be prepared or reached."""


class ShellTransport(Protocol):
    """How a remote executor reaches its far side."""

    name: str

    @property
    def capabilities(self) -> Dict[str, object]: ...

    async def setup(self) -> None: ...

    async def run_script(self, script: str, *, timeout: float) -> Tuple[int, str, str, bool]:
        """Run a bash ``script``; ``(rc, stdout, stderr, timed_out)``."""

    async def close(self) -> None: ...


# --- transports ------------------------------------------------------------------

def _private_dir(path: Path) -> Path:
    """Create ``path`` 0700, or verify an existing one is a real directory we own
    (never a symlink another user planted)."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            raise RemoteShellError(f"control dir {path} is not a private directory of this user")
        os.chmod(path, 0o700)
    return path


def control_dir_for(session_dir: Path) -> Path:
    """The ControlPath directory for one session: ``<session dir>`` itself when
    short enough for a Unix socket, else ``/tmp/polyrob-ssh-<uid>-<hash>``."""
    session_dir = Path(session_dir)
    if len(str(session_dir)) <= _MAX_CONTROL_DIR_LEN:
        session_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(session_dir, 0o700)
        except OSError:
            pass
        return session_dir
    digest = hashlib.sha256(str(session_dir).encode()).hexdigest()[:12]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    base = Path("/tmp") if os.path.isdir("/tmp") else Path(os.path.expanduser("~"))
    return _private_dir(base / f"polyrob-ssh-{uid}-{digest}")


class SshTransport:
    """The ssh side of the remote executor: a persistent ControlMaster session."""

    name = "ssh"

    def __init__(self, *, control_dir: Path, backend=None,
                 runner: Optional[ProcRunner] = None) -> None:
        from tools.code_exec.backends.ssh import SshBackend
        self._backend = backend if backend is not None else SshBackend()
        self._control_dir = Path(control_dir)
        self._runner: ProcRunner = runner or run_group
        self._using_default_runner = runner is None

    @property
    def capabilities(self) -> Dict[str, object]:
        return dict(self._backend.capabilities)

    @property
    def control_path(self) -> str:
        return str(self._control_dir / "ssh-%C")

    def control_options(self) -> Tuple[str, ...]:
        return ("-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.control_path}",
                "-o", "ControlPersist=600")

    def argv(self, remote_command: Optional[str], *extra: str) -> list:
        """PURE: the full ssh argv (``SshBackend.ssh_argv`` + the Control options)."""
        return self._backend.ssh_argv(remote_command,
                                      extra_options=self.control_options() + tuple(extra))

    async def setup(self) -> None:
        if not self._backend.host:
            raise RemoteShellError(
                "SHELL_BACKEND=ssh but CODE_EXEC_SSH_HOST is not set. Set CODE_EXEC_SSH_HOST "
                "(and optionally CODE_EXEC_SSH_USER / CODE_EXEC_SSH_PORT / CODE_EXEC_SSH_KEY).")
        if self._using_default_runner and shutil.which("ssh") is None:
            raise RemoteShellError("SHELL_BACKEND=ssh but the 'ssh' binary is not on PATH.")
        self._backend.ssh_target()  # the '-'-prefixed host/user refusal, before any spawn

    async def run_script(self, script: str, *, timeout: float) -> Tuple[int, str, str, bool]:
        secs = max(1, int(round(float(timeout))))
        remote = (f"timeout --signal=KILL {secs} bash --noprofile --norc -c "
                  f"{shlex.quote(script)}")
        rc, out, err, timed_out = await self._runner(
            self.argv(remote), stdin_bytes=None, timeout=secs + 10, label="ssh")
        # Log host + exit only — never the argv (it carries the key path).
        logger.debug("ssh shell: host=%s exit=%s timed_out=%s", self._backend.host, rc, timed_out)
        return rc, out, err, bool(timed_out or rc in _TIMEOUT_EXIT_CODES)

    async def close(self) -> None:
        """End the ControlMaster (``ssh -O exit``); best-effort."""
        try:
            await self._runner(self.argv(None, "-O", "exit"), stdin_bytes=None, timeout=10,
                               label="ssh")
        except Exception:
            logger.debug("ssh shell: control master exit failed", exc_info=True)


class BackendTransport:
    """Any ``ExecutionBackend`` that runs ``bash`` (the W7 pack backends)."""

    def __init__(self, backend) -> None:
        self._backend = backend
        self.name = getattr(backend, "name", type(backend).__name__)

    @property
    def backend(self):
        return self._backend

    @property
    def capabilities(self) -> Dict[str, object]:
        try:
            return dict(self._backend.capabilities or {})
        except Exception:
            return {}

    async def setup(self) -> None:
        await self._backend.setup()

    async def run_script(self, script: str, *, timeout: float) -> Tuple[int, str, str, bool]:
        from tools.code_exec.result import ExecutionRequest
        result = await self._backend.run(ExecutionRequest(
            language="bash", code=script, timeout=timeout, dev_mode=True, ceiling=timeout))
        rc = result.exit_code if result.exit_code is not None else 1
        return rc, result.stdout or "", result.stderr or "", bool(result.timed_out)

    async def spawn(self, script: str) -> None:
        """The job launcher: the backend's detached exec when it has one."""
        detached = getattr(self._backend, "exec_detached", None)
        if callable(detached):
            await detached(script)
            return
        rc, out, err, _ = await self.run_script(script, timeout=30)
        if rc != 0:
            raise RemoteShellError(f"{self.name}: job launch failed (exit {rc}): {err or out}")

    async def close(self) -> None:
        await self._backend.teardown()


# --- the executor ----------------------------------------------------------------

class RemoteShellExecutor:
    """Runs one session's shell commands on a remote transport."""

    def __init__(self, transport, *, kind: str, default_cwd: str, jobs_dir: str) -> None:
        self._transport = transport
        self.kind = kind
        self.default_cwd = default_cwd
        self._jobs_dir = jobs_dir
        self._ready = False
        self._abs_jobs = ""
        self._jobs: Set[str] = set()

    @property
    def transport(self):
        return self._transport

    @property
    def capabilities(self) -> Dict[str, object]:
        return self._transport.capabilities

    async def _prepare(self) -> None:
        if self._ready:
            return
        await self._transport.setup()
        q = shlex.quote
        rc, out, err, _ = await self._transport.run_script(
            f"mkdir -p {q(self.default_cwd)} {q(self._jobs_dir)} && chmod 700 {q(self._jobs_dir)}"
            f" && cd {q(self._jobs_dir)} && pwd",
            timeout=30)
        jobs = (out or "").strip().splitlines()
        if rc != 0 or not jobs or not jobs[-1].startswith("/"):
            raise RemoteShellError(
                f"{self.kind}: could not prepare the remote session dirs (exit {rc}): "
                f"{(err or out).strip()[:300]}")
        # Every job path is ABSOLUTE from here on: a job's cwd moves, its files do not.
        self._abs_jobs = jobs[-1]
        self._ready = True

    # --- foreground ---------------------------------------------------------------
    async def run_foreground(self, command: str, state: ShellState, *, timeout: float,
                             workdir: str = "") -> Tuple[str, ShellState, int]:
        await self._prepare()
        script = wrap_command(command, state, fallback_cwd=self.default_cwd, workdir=workdir)
        rc, out, err, timed_out = await self._transport.run_script(script, timeout=timeout)
        clean, new_state = parse_state(out or "", state)
        if err:
            clean = f"{clean}\n[stderr]\n{err}" if clean else f"[stderr]\n{err}"
        if timed_out:
            rc = 124
            clean = (clean + "\n" if clean else "") + (
                f"[timed out after {timeout:g}s; remote process group killed]")
        return clean, new_state, rc

    # --- background ---------------------------------------------------------------
    # The job plumbing is tools/shell/jobs.py's — the SAME launcher (pid/log/rc,
    # the stdin FIFO + keeper, jpid) the container and the host run — wrapped in a
    # ``nohup setsid`` so it outlives the ssh session / the sandbox exec.

    def _files(self, job_id: str) -> Dict[str, str]:
        from tools.shell.jobs import job_files
        return job_files(self._abs_jobs or self._jobs_dir, _safe_job_id(job_id))

    def launcher(self, command: str, job_id: str, state: ShellState, *,
                 workdir: str = "") -> str:
        """PURE: the far-side script that writes the job files and detaches the
        shared launcher (unit-testable)."""
        from tools.shell.jobs import command_file_text, launcher_script
        f = self._files(job_id)
        q = shlex.quote
        launch_f = f["cmd"][: -len(".cmd")] + ".launch"
        exports = "".join(f"export {k}={q(v)}\n" for k, v in state.env.items())
        inner = launcher_script(shell="bash --noprofile --norc", files=f, exports=exports)
        return (
            f"mkdir -p {q(self._abs_jobs or self._jobs_dir)} || exit 3\n"
            f"rm -f {q(f['rc'])} {q(f['pid'])}\n"
            f"printf '%s' {q(command_file_text(command, workdir))} > {q(f['cmd'])}\n"
            f"printf '%s' {q(inner)} > {q(launch_f)}\n"
            f"cd {q(state.cwd)} 2>/dev/null || cd {q(self.default_cwd)} 2>/dev/null\n"
            "if command -v setsid >/dev/null 2>&1; then __s=setsid; else __s=; fi\n"
            f"nohup $__s bash --noprofile --norc {q(launch_f)} > /dev/null 2>&1 < /dev/null &\n"
            f"__i=0; while [ $__i -lt 50 ] && [ ! -s {q(f['pid'])} ]; do "
            "sleep 0.1; __i=$((__i+1)); done\n"
            "echo STARTED\n"
        )

    async def start_background(self, command: str, job_id: str, state: ShellState, *,
                               workdir: str = "", pty: bool = False) -> None:
        if pty:
            raise ValueError(f"pty=True runs only on the host executor (posture 3), not on "
                             f"the {self.kind} backend")
        await self._prepare()
        script = self.launcher(command, job_id, state, workdir=workdir)
        spawn = getattr(self._transport, "spawn", None)
        if callable(spawn):
            await spawn(script)
        else:
            rc, out, err, _ = await self._transport.run_script(script, timeout=30)
            if rc != 0:
                raise RemoteShellError(
                    f"{self.kind}: job launch failed (exit {rc}): {(err or out).strip()[:300]}")
        self._jobs.add(job_id)

    async def _one_liner(self, code: str, timeout: float = 15) -> str:
        await self._prepare()
        rc, out, _err, _ = await self._transport.run_script(code, timeout=timeout)
        return out or ""

    async def poll(self, job_id: str) -> str:
        f = {k: shlex.quote(v) for k, v in self._files(job_id).items()}
        out = await self._one_liner(
            f"if [ -f {f['rc']} ]; then echo DONE; else "
            f"pid=$(cat {f['pid']} 2>/dev/null); "
            "if [ -z \"$pid\" ]; then echo UNKNOWN; "
            "elif kill -0 \"$pid\" 2>/dev/null; then echo RUNNING; else echo DONE; fi; fi")
        out = out.strip().upper()
        if out.startswith("RUNNING"):
            return "running"
        if out.startswith("DONE"):
            return "done"
        return "unknown"

    async def exit_code(self, job_id: str) -> Optional[int]:
        f = self._files(job_id)
        out = (await self._one_liner(f"cat {shlex.quote(f['rc'])} 2>/dev/null")).strip()
        try:
            return int(out)
        except ValueError:
            return None

    async def read_log(self, job_id: str, *, max_bytes: int = _DEFAULT_LOG_CAP) -> str:
        f = self._files(job_id)
        cap = max(1, min(int(max_bytes), _DEFAULT_LOG_CAP))
        return await self._one_liner(f"tail -c {cap} {shlex.quote(f['log'])} 2>/dev/null")

    async def read_log_from(self, job_id: str, offset: int, *,
                            max_bytes: int = 64 * 1024) -> Tuple[str, int]:
        """New log text since byte ``offset`` -> ``(text, new_offset)`` (073 W4 notify)."""
        log_f = shlex.quote(self._files(job_id)["log"])
        off = max(0, int(offset))
        cap = max(1, int(max_bytes))
        out = await self._one_liner(
            f"sz=$(wc -c < {log_f} 2>/dev/null || echo 0); sz=$(echo $sz); echo \"$sz\"; "
            f"if [ \"$sz\" -gt {off} ]; then tail -c +{off + 1} {log_f} | head -c {cap}; fi")
        head, _, text = out.partition("\n")
        try:
            size = int(head.strip())
        except ValueError:
            return "", off
        if size < off:          # the log was replaced: start over
            return "", 0
        return text, min(size, off + cap)

    async def write_stdin(self, job_id: str, data: bytes) -> Tuple[bool, str]:
        """Send ``data`` to the job's stdin FIFO (073 W4). ``(ok, message)``."""
        import base64
        from tools.shell.jobs import STDIN_MAX_BYTES, STDIN_WRITE_TIMEOUT_SEC
        if len(data) > STDIN_MAX_BYTES:
            return False, f"stdin write is limited to {STDIN_MAX_BYTES} bytes"
        f = {k: shlex.quote(v) for k, v in self._files(job_id).items()}
        b64 = base64.b64encode(data).decode("ascii")
        t = int(STDIN_WRITE_TIMEOUT_SEC)
        out = (await self._one_liner(
            f"if [ -e {f['closed']} ]; then echo CLOSED; "
            f"elif [ ! -p {f['in']} ]; then echo NOFIFO; "
            f"elif [ -e {f['rc']} ]; then echo NOREADER; "
            f"elif printf '%s' '{b64}' | base64 -d | "
            f"(command -v timeout >/dev/null 2>&1 && timeout {t} sh -c 'cat > \"$1\"' _ {f['in']} "
            f"|| sh -c 'cat > \"$1\"' _ {f['in']}); "
            f"then echo WROTE; else echo NOREADER; fi", timeout=t + 5)).strip()
        if out.endswith("WROTE"):
            return True, f"wrote {len(data)} bytes"
        if out.endswith("CLOSED"):
            return False, "stdin of this job is closed (process close was called)"
        if out.endswith("NOFIFO"):
            return False, "this job has no stdin (it was started without one)"
        return False, "the job is not reading its stdin (it may have exited)"

    async def close_stdin(self, job_id: str) -> Tuple[bool, str]:
        """EOF on the job's stdin: stop the keeper, mark the FIFO closed (073 W4)."""
        f = {k: shlex.quote(v) for k, v in self._files(job_id).items()}
        out = (await self._one_liner(
            f"if [ -e {f['closed']} ]; then echo ALREADY; "
            f"elif [ ! -p {f['in']} ]; then echo NOFIFO; "
            f"else touch {f['closed']}; k=$(cat {f['keeper']} 2>/dev/null); "
            f"[ -n \"$k\" ] && kill \"$k\" 2>/dev/null; echo CLOSED; fi")).strip()
        if out.endswith("CLOSED"):
            return True, "stdin closed (EOF sent)"
        if out.endswith("ALREADY"):
            return True, "stdin was already closed"
        return False, "this job has no stdin (it was started without one)"

    async def kill(self, job_id: str) -> bool:
        f = {k: shlex.quote(v) for k, v in self._files(job_id).items()}
        out = await self._one_liner(
            f"pid=$(cat {f['pid']} 2>/dev/null); "
            "if [ -n \"$pid\" ]; then "
            f"j=$(cat {f['jpid']} 2>/dev/null); "
            "kill -TERM -\"$pid\" 2>/dev/null; pkill -TERM -P \"$pid\" 2>/dev/null; "
            "[ -n \"$j\" ] && pkill -TERM -P \"$j\" 2>/dev/null; "
            "kill -TERM \"$pid\" 2>/dev/null; sleep 0.2; "
            "kill -KILL -\"$pid\" 2>/dev/null; pkill -KILL -P \"$pid\" 2>/dev/null; "
            "[ -n \"$j\" ] && pkill -KILL -P \"$j\" 2>/dev/null; "
            "kill -KILL \"$pid\" 2>/dev/null; echo KILLED; else echo NOPID; fi")
        return "KILLED" in out

    async def kill_all(self) -> None:
        for job_id in list(self._jobs):
            try:
                if await self.poll(job_id) == "running":
                    await self.kill(job_id)
            except Exception:
                logger.debug("%s shell: kill of %s failed", self.kind, job_id, exc_info=True)

    async def close(self) -> None:
        """Session end: kill this session's jobs, then release the transport."""
        if self._ready:
            await self.kill_all()
        try:
            await self._transport.close()
        except Exception:
            logger.debug("%s shell: transport close failed", self.kind, exc_info=True)
        self._ready = False


def safe_session_component(session_id: str) -> str:
    """A path component for ``session_id`` (cleaned ids pass; anything else is hashed)."""
    sid = session_id or "shell"
    try:
        return _safe_job_id(sid)
    except ValueError:
        return "s-" + hashlib.sha256(sid.encode()).hexdigest()[:16]


def ssh_shell_executor(*, session_id: str, session_dir: Path,
                       runner: Optional[ProcRunner] = None) -> RemoteShellExecutor:
    """The ``SHELL_BACKEND=ssh`` executor for one session. Remote paths are
    relative to the remote login directory (every ssh command starts there):
    workspace ``polyrob/<sid>``, jobs ``.polyrob-jobs/<sid>``."""
    comp = safe_session_component(session_id)
    transport = SshTransport(control_dir=control_dir_for(Path(session_dir)), runner=runner)
    return RemoteShellExecutor(transport, kind="ssh", default_cwd=f"polyrob/{comp}",
                               jobs_dir=f".polyrob-jobs/{comp}")


def backend_shell_executor(backend, *, kind: str, session_id: str,
                           default_cwd: str = "/workspace",
                           jobs_root: str = "/tmp/polyrob-jobs") -> RemoteShellExecutor:
    """A pack backend's executor: the persistent backend behind a
    :class:`BackendTransport`. Jobs live in ``<jobs_root>/<sid>`` on the far side."""
    comp = safe_session_component(session_id)
    return RemoteShellExecutor(BackendTransport(backend), kind=kind, default_cwd=default_cwd,
                               jobs_dir=f"{jobs_root.rstrip('/')}/{comp}")


__all__ = ["BackendTransport", "RemoteShellError", "RemoteShellExecutor", "ShellTransport",
           "SshTransport", "backend_shell_executor", "control_dir_for",
           "safe_session_component", "ssh_shell_executor"]
