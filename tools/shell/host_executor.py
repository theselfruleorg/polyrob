"""Host shell executor — posture 3 (073 W1).

Runs the agent's shell commands DIRECTLY on the machine that runs the agent, as
the agent's own user, with the real filesystem — no container. Same surface as
``DockerShellExecutor`` so the `shell`/`process` tools stay the only shell tools.

It is never a fallback: ``tools/shell/backend_pool.py::resolve_shell_executor``
builds one only when ``core.security.host_execution.host_shell_refusal(ctx)`` is
None (posture 3 + POLYROB_LOCAL + no in-process signing key + an owner turn at a
foreground terminal). Every other path keeps the sandbox or refuses.

- **foreground**: ``bash -c`` per call in its own process group (the shared
  ``run_group`` — group kill on timeout, bounded capture), the SAME snapshot-replay
  ``ShellState`` the container uses, and a scrubbed env (``build_child_env``: an
  allowlist; no ``*_API_KEY``/``*_TOKEN``/seed ever reaches the child).
- **background**: a ``setsid`` launcher (``tools/shell/jobs.py``, the same text
  the container runs) writes ``pid``/``log``/``rc`` under the session
  ``logs/jobs/`` dir (``pm()``), never ``/tmp``; the launcher is the group leader,
  so a kill reaps the whole job tree. The exit code survives in ``rc``. Stdin is
  a FIFO held open by a keeper (073 W4: ``write_stdin``/``close_stdin``).
- **pty** (073 W4, host only): ``pty.openpty``; the job is a session leader with
  the pty as its controlling terminal; a daemon pump copies its output to the log.

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from tools.code_exec.backends._proc import run_group
from tools.code_exec.env_policy import build_child_env
from tools.shell.executor import _DEFAULT_LOG_CAP, _MAX_LOG_CAP, _safe_job_id
from tools.shell.state import ShellState, parse_state, wrap_command

#: Host vars a developer shell needs beyond the base allowlist. Never a secret:
#: build_child_env drops any secret-NAMED var whatever list named it.
_HOST_EXTRA_ALLOWLIST = ("LOGNAME", "COLUMNS", "LINES", "EDITOR", "VISUAL", "PAGER",
                         "LC_MESSAGES", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                         "XDG_DATA_HOME", "VIRTUAL_ENV", "CONDA_PREFIX", "GOPATH",
                         "JAVA_HOME", "NVM_DIR", "PYENV_ROOT", "CARGO_HOME", "RUSTUP_HOME")


def host_child_env() -> Dict[str, str]:
    """The scrubbed host env. ``SHELL_HOST_FORWARD_AGENT`` (default OFF) adds
    ``SSH_AUTH_SOCK`` so `git`/`ssh` can use the owner's ssh-agent — the agent
    socket signs, it never hands out a key."""
    from core.env import bool_env
    extra = _HOST_EXTRA_ALLOWLIST
    if bool_env("SHELL_HOST_FORWARD_AGENT", False):
        extra = extra + ("SSH_AUTH_SOCK",)
    return build_child_env({"PAGER": "cat", "GIT_PAGER": "cat", "TERM": "dumb"},
                           extra_allowlist=extra)


def host_init_prelude() -> str:
    """``SHELL_HOST_INIT_FILES`` (comma list, ``~`` expanded; default empty): files
    each host command sources first (like a ``shell_init_files`` list) — e.g. the
    owner's ``~/.bashrc`` for nvm/pyenv PATH. Missing files are skipped; output and
    errors of the sourcing are discarded. ⚠️ Whatever those files export (an API
    key in ``.bashrc``) becomes visible to the agent's commands."""
    raw = os.getenv("SHELL_HOST_INIT_FILES") or ""
    lines = []
    for item in raw.split(","):
        path = os.path.expanduser(item.strip())
        if path and os.path.isfile(path):
            lines.append(f". {shlex.quote(path)} >/dev/null 2>&1 || true")
    return ("\n".join(lines) + "\n") if lines else ""


class HostShellExecutor:
    """Runs shell commands on the host for ONE session (posture 3 only)."""

    kind = "host"

    def __init__(self, *, workspace_dir: str, jobs_dir: Path, bash: str = "bash"):
        self.default_cwd = str(workspace_dir or os.getcwd())
        self._jobs_dir = Path(jobs_dir)
        self._bash = bash
        self._procs: Dict[str, subprocess.Popen] = {}
        self._ptys: Dict[str, int] = {}   # job id -> pty master fd (073 W4)

    # --- foreground -------------------------------------------------------------
    async def run_foreground(self, command: str, state: ShellState, *, timeout: float,
                             workdir: str = "", stdin_bytes: Optional[bytes] = None,
                             ) -> Tuple[str, ShellState, int]:
        """``stdin_bytes`` (073 W5) is the ONLY way a sudo password reaches a
        command: stdin of this one call, never env, argv or a file."""
        script = host_init_prelude() + wrap_command(
            command, state, fallback_cwd=self.default_cwd, workdir=workdir)
        start = state.cwd if os.path.isdir(state.cwd) else self.default_cwd
        rc, out, err, timed_out = await run_group(
            [self._bash, "--noprofile", "--norc", "-c", script],
            stdin_bytes=stdin_bytes, timeout=timeout, label="host shell",
            env=host_child_env(), cwd=start,
        )
        clean, new_state = parse_state(out or "", state)
        if err:
            clean = f"{clean}\n[stderr]\n{err}" if clean else f"[stderr]\n{err}"
        if timed_out:
            rc = 124
            clean = (clean + "\n" if clean else "") + f"[timed out after {timeout:g}s; process group killed]"
        return clean, new_state, rc if rc is not None else 0

    # --- background -------------------------------------------------------------
    def _paths(self, job_id: str) -> Tuple[Path, Path, Path, Path]:
        job_id = _safe_job_id(job_id)
        d = self._jobs_dir
        return d / f"{job_id}.pid", d / f"{job_id}.log", d / f"{job_id}.cmd", d / f"{job_id}.rc"

    def _files(self, job_id: str) -> Dict[str, str]:
        from tools.shell.jobs import job_files
        return job_files(str(self._jobs_dir), _safe_job_id(job_id))

    def _prepare_jobs_dir(self) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._jobs_dir, 0o700)
        except OSError:
            pass

    async def start_background(self, command: str, job_id: str, state: ShellState, *,
                               workdir: str = "", pty: bool = False) -> None:
        """Launch ``command`` detached. ``pty=True`` (073 W4, host only) gives the
        job a pseudo-terminal: its output is the log, ``process write`` types
        into it. Without a pty the job's stdin is the FIFO of
        ``tools/shell/jobs.py``."""
        from tools.shell.jobs import command_file_text, launcher_script
        pid_f, log_f, cmd_f, _rc_f = self._paths(job_id)
        self._prepare_jobs_dir()
        cwd = state.cwd if os.path.isdir(state.cwd) else self.default_cwd
        exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in state.env.items())
        if pty:
            cmd_f.write_text(host_init_prelude() + exports + command_file_text(command, workdir), encoding="utf-8")
            self._start_pty(job_id, cwd)
        else:
            cmd_f.write_text(host_init_prelude() + command_file_text(command, workdir), encoding="utf-8")
            shell = f"{shlex.quote(self._bash)} --noprofile --norc"
            launcher = launcher_script(shell=shell, files=self._files(job_id), exports=exports)
            proc = subprocess.Popen(
                [self._bash, "--noprofile", "--norc", "-c", launcher],
                env=host_child_env(), cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,  # the launcher heads its own group -> tree kill
            )
            self._procs[job_id] = proc
        # The pid file is written by the launcher; wait briefly so an immediate
        # poll/kill sees it.
        for _ in range(50):
            if pid_f.exists():
                break
            await asyncio.sleep(0.01)

    def _start_pty(self, job_id: str, cwd: str) -> None:
        """One job on a pseudo-terminal: ``pty.openpty`` + a session leader with
        the pty as its controlling terminal. A small daemon pump copies the pty
        output to the log and writes ``.rc`` when the job exits."""
        import pty as _pty
        import threading
        pid_f, log_f, cmd_f, rc_f = self._paths(job_id)
        master, slave = _pty.openpty()

        def _ctty():  # runs in the child after setsid (start_new_session)
            try:
                import fcntl
                import termios
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except Exception:
                pass

        env = host_child_env()
        env["TERM"] = "xterm-256color"
        try:
            proc = subprocess.Popen(
                [self._bash, "--noprofile", "--norc", str(cmd_f)],
                env=env, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, preexec_fn=_ctty, close_fds=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        pid_f.write_text(str(proc.pid), encoding="utf-8")
        self._procs[job_id] = proc
        self._ptys[job_id] = master

        def _pump():
            import select
            with open(log_f, "ab", buffering=0) as log:
                while True:
                    try:
                        ready, _, _ = select.select([master], [], [], 0.2)
                    except (OSError, ValueError):
                        break
                    if ready:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:  # EIO: every slave fd closed
                            chunk = b""
                        if chunk:
                            log.write(chunk)
                            continue
                        break  # EOF/EIO: no process holds the terminal any more
                    elif proc.poll() is not None:
                        break
            rc = proc.wait()
            try:
                rc_f.write_text(str(rc if rc >= 0 else 128 - rc), encoding="utf-8")
            except OSError:
                pass
            fd = self._ptys.pop(job_id, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        log_f.write_bytes(b"")
        threading.Thread(target=_pump, name=f"pty-{job_id}", daemon=True).start()

    def _pid(self, job_id: str) -> Optional[int]:
        pid_f = self._paths(job_id)[0]
        try:
            return int(pid_f.read_text().strip())
        except (OSError, ValueError):
            proc = self._procs.get(job_id)
            return proc.pid if proc is not None else None

    async def poll(self, job_id: str) -> str:
        _, _, _, rc_f = self._paths(job_id)
        proc = self._procs.get(job_id)
        if proc is not None:
            return "running" if proc.poll() is None else "done"
        if rc_f.exists():
            return "done"
        pid = self._pid(job_id)
        if pid is None:
            return "unknown"
        try:
            os.kill(pid, 0)
            return "running"
        except ProcessLookupError:
            return "done"
        except PermissionError:
            return "running"

    async def exit_code(self, job_id: str) -> Optional[int]:
        rc_f = self._paths(job_id)[3]
        try:
            return int(rc_f.read_text().strip())
        except (OSError, ValueError):
            return None

    async def read_log(self, job_id: str, *, max_bytes: int = _DEFAULT_LOG_CAP) -> str:
        log_f = self._paths(job_id)[1]
        cap = max(1, min(int(max_bytes), _MAX_LOG_CAP))
        try:
            with open(log_f, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - cap))
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    async def kill(self, job_id: str) -> bool:
        pid = self._pid(job_id)
        if pid is None:
            return False
        killed = False
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
                killed = True
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, sig)
                    killed = True
                except (ProcessLookupError, PermissionError):
                    pass
            if sig == signal.SIGTERM:
                await asyncio.sleep(0.2)
        proc = self._procs.get(job_id)
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        return killed

    async def read_log_from(self, job_id: str, offset: int, *,
                            max_bytes: int = 64 * 1024) -> Tuple[str, int]:
        """New log text since byte ``offset`` -> ``(text, new_offset)`` (073 W4)."""
        log_f = self._paths(job_id)[1]
        off = max(0, int(offset))
        try:
            with open(log_f, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size < off:
                    return "", 0
                fh.seek(off)
                data = fh.read(max(1, int(max_bytes)))
        except OSError:
            return "", off
        return data.decode("utf-8", errors="replace"), off + len(data)

    def is_pty(self, job_id: str) -> bool:
        return job_id in self._ptys

    async def write_stdin(self, job_id: str, data: bytes) -> Tuple[bool, str]:
        """Send ``data`` to the job: the pty master for a pty job, else the stdin
        FIFO (073 W4). Non-blocking with a deadline — a job that does not read
        never hangs the turn. ``(ok, message)``."""
        import errno
        import time
        from tools.shell.jobs import STDIN_MAX_BYTES, STDIN_WRITE_TIMEOUT_SEC
        if len(data) > STDIN_MAX_BYTES:
            return False, f"stdin write is limited to {STDIN_MAX_BYTES} bytes"
        files = self._files(job_id)
        master = self._ptys.get(_safe_job_id(job_id))
        own_fd = False
        if master is not None:
            fd = master
        else:
            if os.path.exists(files["closed"]):
                return False, "stdin of this job is closed (process close was called)"
            if not os.path.exists(files["in"]):
                return False, "this job has no stdin (it was started without one)"
            try:
                fd = os.open(files["in"], os.O_WRONLY | os.O_NONBLOCK)
                own_fd = True
            except OSError as e:
                if e.errno == errno.ENXIO:
                    return False, "the job is not reading its stdin (it may have exited)"
                return False, f"could not open the job's stdin: {e.strerror}"
        try:
            if master is not None:
                os.set_blocking(fd, False)
            sent = 0
            deadline = time.monotonic() + STDIN_WRITE_TIMEOUT_SEC
            while sent < len(data):
                try:
                    sent += os.write(fd, data[sent:])
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False, (f"wrote {sent} of {len(data)} bytes; the job is not "
                                       "reading its input")
                    await asyncio.sleep(0.05)
                except OSError as e:
                    return False, f"write failed after {sent} bytes: {e.strerror}"
            return True, f"wrote {sent} bytes"
        finally:
            if own_fd:
                os.close(fd)

    async def close_stdin(self, job_id: str) -> Tuple[bool, str]:
        """EOF (073 W4): Ctrl-D on a pty; for a FIFO, stop the keeper so no writer
        is left. ``(ok, message)``."""
        files = self._files(job_id)
        master = self._ptys.get(_safe_job_id(job_id))
        if master is not None:
            try:
                os.write(master, b"\x04")
                return True, "sent EOF (Ctrl-D) to the terminal"
            except OSError as e:
                return False, f"could not send EOF: {e.strerror}"
        if os.path.exists(files["closed"]):
            return True, "stdin was already closed"
        if not os.path.exists(files["in"]):
            return False, "this job has no stdin (it was started without one)"
        Path(files["closed"]).touch()
        try:
            kpid = int(Path(files["keeper"]).read_text().strip())
            os.kill(kpid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        return True, "stdin closed (EOF sent)"

    async def kill_all(self) -> None:
        for job_id in list(self._procs):
            proc = self._procs[job_id]
            if proc.poll() is None:
                await self.kill(job_id)


__all__ = ["HostShellExecutor", "host_child_env"]
