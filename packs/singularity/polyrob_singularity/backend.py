"""The Singularity / Apptainer execution backend (073 W7): an instance per session.

CLI only — no SDK, no extra. The binary is ``SINGULARITY_BINARY`` when set, else
``apptainer``, else ``singularity`` on PATH.

- Image: ``SINGULARITY_IMAGE`` (default ``docker://python:3.12-slim``; a local
  ``.sif`` path works too).
- Session: ``instance start --containall --no-home --cleanenv`` with the session
  workspace bound at ``/workspace`` (the files ARE the host workspace — no sync);
  each call is ``exec instance://<name>``; teardown is ``instance stop``.
  Ephemeral (no session): one ``exec`` per run with the same isolation flags.
- Network: the docker rule (``CODE_EXEC_NETWORK``; a dev sandbox is networked) —
  ``--net --network none`` otherwise.
- Env: the CLI runs with the scrubbed child env (``build_child_env``) and
  ``--cleanenv``; only ``request.env`` minus secret-named vars is passed
  (``--env``).

⚠️ Isolation is weaker than the docker backend's: the container runs as the
invoking user on the host kernel (no capability drop beyond an unprivileged
user's, no pids/memory cap). It is a sandbox for the single-user operator and an
HPC host, which is the shape this backend exists for.
"""
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.backends._proc import run_group
from tools.code_exec.backends.remote_sandbox import RemoteSandboxBackend, session_slug

logger = logging.getLogger(__name__)


def singularity_binary() -> str:
    explicit = (os.getenv("SINGULARITY_BINARY") or "").strip()
    if explicit:
        if explicit.startswith("-"):
            raise ExecutionBackendError(
                f"SINGULARITY_BINARY value {explicit!r} begins with '-'; set a binary name or path.")
        return explicit
    for name in ("apptainer", "singularity"):
        if shutil.which(name):
            return name
    return "apptainer"


class SingularityBackend(RemoteSandboxBackend):
    name = "singularity"
    isolation = "container-unprivileged"
    extra = ""
    workdir = "/workspace"

    def __init__(self, *, session_id: Optional[str] = None, dev_mode: bool = False,
                 workspace_dir: str = "", runner=None) -> None:
        super().__init__(session_id=session_id, dev_mode=dev_mode)
        self._workspace_dir = workspace_dir
        self._runner = runner or run_group
        self._using_default_runner = runner is None
        self._tmp: Optional[str] = None

    @property
    def image(self) -> str:
        return os.getenv("SINGULARITY_IMAGE") or "docker://python:3.12-slim"

    def instance_name(self) -> str:
        return "polyrob-" + session_slug(self.session_id)

    def _host_workdir(self) -> str:
        if self._workspace_dir:
            os.makedirs(self._workspace_dir, exist_ok=True)
            return self._workspace_dir
        if self.session_id:
            try:
                from agents.task.path import pm
                path = str(pm().get_workspace_dir(self.session_id))
                os.makedirs(path, exist_ok=True)
                return path
            except Exception:
                logger.debug("singularity backend: no session workspace", exc_info=True)
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="rob_singularity_")
        return self._tmp

    def isolation_flags(self, workdir_host: str) -> List[str]:
        """PURE (env-read only): the flags both modes share."""
        if "," in workdir_host or ":" in workdir_host:
            raise ExecutionBackendError(
                f"singularity backend: workspace path {workdir_host!r} holds ',' or ':'")
        flags = ["--containall", "--no-home", "--cleanenv",
                 "--bind", f"{workdir_host}:{self.workdir}"]
        if not self.network_enabled():
            flags += ["--net", "--network", "none"]
        return flags

    async def _cli(self, args: List[str], *, timeout: float, stdin: Optional[str] = None
                   ) -> Tuple[int, str, str]:
        rc, out, err, timed_out = await self._runner(
            [singularity_binary(), *args],
            stdin_bytes=stdin.encode() if stdin is not None else None,
            timeout=timeout, label="singularity")
        if timed_out:
            return 124, out, err or f"singularity: timed out after {timeout:g}s"
        return rc, out, err

    async def _provision(self) -> Any:
        binary = singularity_binary()
        if self._using_default_runner and shutil.which(binary) is None and not os.path.isfile(binary):
            raise ExecutionBackendError(
                f"the singularity execution backend needs '{binary}' on PATH (install "
                "Apptainer or SingularityCE, or set SINGULARITY_BINARY).")
        workdir = self._host_workdir()
        if not self.persistent:
            return {"workdir": workdir, "instance": None}
        name = self.instance_name()
        rc, out, err = await self._cli(
            ["instance", "start", *self.isolation_flags(workdir), self.image, name],
            timeout=600)
        if rc != 0 and "already exists" not in (err + out).lower():
            raise ExecutionBackendError(
                f"singularity instance start failed (exit {rc}): {(err or out).strip()[:300]}")
        return {"workdir": workdir, "instance": name}

    def _env_flags(self, env: Dict[str, str]) -> List[str]:
        flags: List[str] = []
        for k, v in env.items():
            flags += ["--env", f"{k}={v}"]
        return flags

    async def _exec(self, argv: List[str], *, timeout: float, stdin: Optional[str],
                    env: Dict[str, str]) -> Tuple[int, str, str]:
        handle = self._handle
        if handle["instance"]:
            args = ["exec", "--cleanenv", "--pwd", self.workdir, *self._env_flags(env),
                    f"instance://{handle['instance']}", *argv]
        else:
            args = ["exec", *self.isolation_flags(handle["workdir"]), "--pwd", self.workdir,
                    *self._env_flags(env), self.image, *argv]
        return await self._cli(args, timeout=timeout, stdin=stdin)

    async def _release(self, handle: Any) -> None:
        if handle.get("instance"):
            await self._cli(["instance", "stop", handle["instance"]], timeout=60)
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


def shell_executor(*, session_id: str, workspace_dir: str = "", logs_dir: str = ""):
    """``SHELL_BACKEND=singularity``: the session's instance behind the remote executor."""
    from tools.shell.remote_executor import backend_shell_executor
    backend = SingularityBackend(session_id=session_id, dev_mode=True,
                                 workspace_dir=workspace_dir)
    return backend_shell_executor(backend,
                                  kind="singularity", session_id=session_id,
                                  default_cwd=SingularityBackend.workdir)


__all__ = ["SingularityBackend", "shell_executor", "singularity_binary"]
