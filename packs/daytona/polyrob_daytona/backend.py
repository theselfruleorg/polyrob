"""The Daytona execution backend (073 W7): a Daytona sandbox per session.

- SDK: ``daytona`` (older releases: ``daytona_sdk``) — the ``daytona`` extra,
  imported only in ``setup()``.
- Credentials: ``DAYTONA_API_KEY`` (+ optional ``DAYTONA_API_URL``,
  ``DAYTONA_TARGET``) configure the client in THIS process; never forwarded.
- Image: ``DAYTONA_SANDBOX_IMAGE`` (empty = the provider's default snapshot).
- Persistence: ``DAYTONA_SANDBOX_PERSIST`` (default OFF). On teardown of a session
  sandbox the sandbox is STOPPED (not deleted) and its id kept locally; the next
  setup for the same session STARTS it again (files and installs survive; running
  processes do not). Off: the sandbox is deleted.
- Network: the docker rule (``CODE_EXEC_NETWORK``; a dev sandbox is networked) —
  ``network_block_all=True`` otherwise.
- Labels: ``polyrob.session=<session slug>`` on every sandbox it creates.
- Paths: the shell's workspace is ``~/workspace`` and its job files
  ``/tmp/polyrob-jobs/<sid>`` (every exec starts in the sandbox user's home).
"""
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from core.env import bool_env
from tools.code_exec.backend import ExecutionBackendError
from tools.code_exec.backends.remote_sandbox import (RemoteSandboxBackend, env_value,
                                                     require_sdk, session_slug)

logger = logging.getLogger(__name__)


class DaytonaBackend(RemoteSandboxBackend):
    name = "daytona"
    isolation = "vm"
    extra = "daytona"
    #: RELATIVE to the sandbox user's home, where every exec starts (the default
    #: user is not root, so ``/workspace`` may not be creatable).
    workdir = "workspace"

    def _sdk(self):
        return require_sdk(("daytona", "daytona_sdk"), extra="daytona",
                           what="the daytona execution backend")

    def _client(self, sdk):
        key = env_value("DAYTONA_API_KEY")
        if not key:
            raise ExecutionBackendError(
                "the daytona execution backend needs DAYTONA_API_KEY (the Daytona API key).")
        kwargs: Dict[str, Any] = {"api_key": key}
        url = env_value("DAYTONA_API_URL")
        target = env_value("DAYTONA_TARGET")
        if url:
            kwargs["api_url"] = url
        if target:
            kwargs["target"] = target
        return sdk.Daytona(sdk.DaytonaConfig(**kwargs))

    def _params(self, sdk):
        labels = {"polyrob.session": session_slug(self.session_id)}
        common: Dict[str, Any] = {"labels": labels,
                                  "network_block_all": not self.network_enabled()}
        image = os.getenv("DAYTONA_SANDBOX_IMAGE") or ""
        if image and hasattr(sdk, "CreateSandboxFromImageParams"):
            return sdk.CreateSandboxFromImageParams(image=image, **common)
        if hasattr(sdk, "CreateSandboxFromSnapshotParams"):
            return sdk.CreateSandboxFromSnapshotParams(**common)
        params_cls = getattr(sdk, "CreateSandboxParams")
        return params_cls(image=image, **common) if image else params_cls(**common)

    def _persist(self) -> bool:
        return self.persistent and bool_env("DAYTONA_SANDBOX_PERSIST", False)

    async def _provision(self) -> Any:
        sdk = self._sdk()

        def _create():
            client = self._client(sdk)
            sandbox = None
            prior = self.load_state().get("sandbox_id") if self._persist() else None
            if prior:
                try:
                    sandbox = client.get(prior)
                    state = str(getattr(sandbox, "state", "") or "").lower()
                    if "started" not in state:
                        start = getattr(sandbox, "start", None)
                        start() if callable(start) else client.start(sandbox)
                except Exception:
                    logger.warning("daytona backend: could not resume sandbox %s; creating "
                                   "a new one", prior, exc_info=True)
                    sandbox = None
            if sandbox is None:
                sandbox = client.create(self._params(sdk))
            if self._persist():
                self.save_state({"sandbox_id": getattr(sandbox, "id", "")})
            sandbox.process.exec(f"mkdir -p {self.workdir}")
            return {"client": client, "sandbox": sandbox}

        return await self.in_thread(_create)

    async def _exec(self, argv: List[str], *, timeout: float, stdin: Optional[str],
                    env: Dict[str, str]) -> Tuple[int, str, str]:
        sandbox = self._handle["sandbox"]
        command = self.shell_join(argv)
        if stdin:
            # The process API takes no stdin stream: feed it through a here-string.
            command = f"printf '%s' {self.shell_join([stdin])} | {command}"
            command = f"bash -c {self.shell_join([command])}"

        def _run():
            kwargs: Dict[str, Any] = {"timeout": int(timeout)}
            if env:
                kwargs["env"] = dict(env)
            return sandbox.process.exec(command, **kwargs)

        resp = await self.in_thread(_run)
        rc = getattr(resp, "exit_code", None)
        out = getattr(resp, "result", None)
        if out is None:
            artifacts = getattr(resp, "artifacts", None)
            out = getattr(artifacts, "stdout", "") if artifacts is not None else ""
        # Daytona returns one combined stream; it is reported as stdout.
        return (rc if rc is not None else 1), str(out or ""), ""

    async def _release(self, handle: Any) -> None:
        client, sandbox = handle["client"], handle["sandbox"]

        def _stop():
            if self._persist():
                stop = getattr(sandbox, "stop", None)
                stop() if callable(stop) else client.stop(sandbox)
                return
            delete = getattr(sandbox, "delete", None)
            delete() if callable(delete) else client.delete(sandbox)
            self.clear_state()

        await self.in_thread(_stop)


def shell_executor(*, session_id: str, workspace_dir: str = "", logs_dir: str = ""):
    """``SHELL_BACKEND=daytona``: the session's Daytona sandbox behind the remote executor."""
    from tools.shell.remote_executor import backend_shell_executor
    return backend_shell_executor(DaytonaBackend(session_id=session_id, dev_mode=True),
                                  kind="daytona", session_id=session_id,
                                  default_cwd=DaytonaBackend.workdir)


__all__ = ["DaytonaBackend", "shell_executor"]
