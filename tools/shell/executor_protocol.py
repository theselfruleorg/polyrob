"""The ONE shell-executor contract (073 W6, §4 principle 1).

`shell` and `process` stay the only shell tools; WHERE a command runs is an
executor that satisfies :class:`ShellExecutor`. Implementations:

- ``DockerShellExecutor`` (``tools/shell/executor.py``) — the session's
  persistent hardened container;
- ``HostShellExecutor`` (``tools/shell/host_executor.py``) — posture 3, never a
  fallback;
- ``RemoteShellExecutor`` (``tools/shell/remote_executor.py``) — ssh
  (ControlMaster) and every pack execution backend (Modal, Daytona, Vercel
  Sandbox, Singularity) over one transport seam.

``tools/shell/backend_pool.py::resolve_shell_executor`` picks one per call and
refuses anything that does not satisfy this protocol (:func:`require_shell_executor`).

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

from typing import Optional, Protocol, Tuple, runtime_checkable

from tools.shell.state import ShellState

#: The methods every executor carries (``kind`` and ``default_cwd`` are attributes).
EXECUTOR_METHODS = ("run_foreground", "start_background", "poll", "read_log", "kill",
                    "exit_code", "read_log_from", "write_stdin", "close_stdin")


@runtime_checkable
class ShellExecutor(Protocol):
    """Where one session's shell commands run."""

    #: ``"docker"`` | ``"host"`` | ``"ssh"`` | a pack backend name. The `process`
    #: tool pins a job to the executor kind that launched it.
    kind: str
    #: The cwd of a fresh session state.
    default_cwd: str

    async def run_foreground(self, command: str, state: ShellState, *, timeout: float,
                             workdir: str = "") -> Tuple[str, ShellState, int]:
        """Run ``command`` with the persisted cwd/env; ``(clean output, new state, rc)``."""

    async def start_background(self, command: str, job_id: str, state: ShellState, *,
                               workdir: str = "", pty: bool = False) -> None:
        """Launch ``command`` detached; it survives the call. ``workdir`` runs this one
        job elsewhere; ``pty`` is host-only (other executors raise ``ValueError``)."""

    async def poll(self, job_id: str) -> str:
        """``"running"`` | ``"done"`` | ``"unknown"``."""

    async def read_log(self, job_id: str, *, max_bytes: int = ...) -> str:
        """The tail of a background job's log."""

    async def kill(self, job_id: str) -> bool:
        """Tree-kill a background job."""

    async def exit_code(self, job_id: str) -> Optional[int]:
        """The job's exit code once done; ``None`` = unknown."""

    async def read_log_from(self, job_id: str, offset: int, *,
                            max_bytes: int = ...) -> Tuple[str, int]:
        """New log text since byte ``offset`` -> ``(text, new offset)`` (073 W4 notify)."""

    async def write_stdin(self, job_id: str, data: bytes) -> Tuple[bool, str]:
        """Send bytes to the job's stdin FIFO -> ``(ok, message)``."""

    async def close_stdin(self, job_id: str) -> Tuple[bool, str]:
        """EOF on the job's stdin -> ``(ok, message)``."""


class NotAShellExecutor(TypeError):
    """A backend factory returned something that is not a :class:`ShellExecutor`."""


def is_shell_executor(obj) -> bool:
    """True when ``obj`` carries the whole contract (attributes + async methods)."""
    if not isinstance(getattr(obj, "kind", None), str) or not getattr(obj, "kind"):
        return False
    if not isinstance(getattr(obj, "default_cwd", None), str):
        return False
    return all(callable(getattr(obj, m, None)) for m in EXECUTOR_METHODS)


def require_shell_executor(obj, *, source: str = ""):
    """Return ``obj`` or raise :class:`NotAShellExecutor` naming what is missing."""
    if is_shell_executor(obj):
        return obj
    missing = [m for m in ("kind", "default_cwd", *EXECUTOR_METHODS) if not hasattr(obj, m)]
    where = f" from {source}" if source else ""
    raise NotAShellExecutor(
        f"{type(obj).__name__}{where} is not a shell executor (missing: "
        f"{', '.join(missing) or 'a non-empty kind / a str default_cwd'})")


__all__ = ["EXECUTOR_METHODS", "NotAShellExecutor", "ShellExecutor", "is_shell_executor",
           "require_shell_executor"]
