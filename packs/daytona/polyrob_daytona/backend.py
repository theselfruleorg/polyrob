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
            prior = self.load_state()
            if self._persist() and prior.get("sandbox_id"):
                sandbox = self._resume(client, prior["sandbox_id"])
            elif prior.get("mode") == "ephemeral" and prior.get("sandbox_id"):
                self._reap_dead_owner(client, prior)
            if sandbox is None:
                sandbox = client.create(self._params(sdk))
            # 098 CL-15: a record names the sandbox BEFORE anything else can fail.
            from polyrob.sdk import process_identity
            self.save_state({"phase": "created", "sandbox_id": getattr(sandbox, "id", ""),
                             "mode": "persist" if self._persist() else "ephemeral",
                             "owner": process_identity().encode()})
            try:
                sandbox.process.exec(f"mkdir -p {self.workdir}")
            except BaseException:
                self._release_sync(client, sandbox)  # never a billed sandbox with no handle
                raise
            return {"client": client, "sandbox": sandbox}

        return await self.in_thread(_create)

    def _resume(self, client, prior: str):
        """The persisted sandbox, or None when the provider says it is gone. Any
        other lookup error raises: never a silent second sandbox (098 CL-15)."""
        try:
            sandbox = client.get(prior)
        except Exception as e:
            if not _not_found(e):
                raise ExecutionBackendError(
                    f"daytona backend: could not look up sandbox {prior} ({e}); not creating "
                    "a second one — retry, or delete it in Daytona") from e
            logger.warning("daytona backend: sandbox %s is gone; creating a new one", prior)
            self.clear_state()
            return None
        state = str(getattr(sandbox, "state", "") or "").lower()
        if "started" not in state:
            start = getattr(sandbox, "start", None)
            start() if callable(start) else client.start(sandbox)
        return sandbox

    def _reap_dead_owner(self, client, prior: Dict[str, Any]) -> None:
        """An ephemeral sandbox recorded by a process that is gone: delete it."""
        from polyrob.sdk import ProcIdentity, is_alive
        owner = ProcIdentity.decode(prior.get("owner"))
        if owner is not None and is_alive(owner):
            return
        try:
            old = client.get(prior["sandbox_id"])
            delete = getattr(old, "delete", None)
            delete() if callable(delete) else client.delete(old)
        except Exception:
            logger.warning("daytona backend: could not delete orphaned sandbox %s",
                           prior.get("sandbox_id"), exc_info=True)

    def reap_orphan(self, record: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """The boot sweep's verb (098 CL-15) for a record whose owner is dead:
        persist -> stop (the record stays, ``phase="stopped"``, so the session
        resumes it); ephemeral -> delete (the record goes). A provider
        not-found drops the record; any other error raises."""
        client = self._client(self._sdk())
        sid = record["sandbox_id"]
        try:
            sandbox = client.get(sid)
        except Exception as e:
            if _not_found(e):
                return "gone", None
            raise
        if record.get("mode", "persist") == "persist":
            stop = getattr(sandbox, "stop", None)
            stop() if callable(stop) else client.stop(sandbox)
            return "stopped", dict(record, phase="stopped")
        delete = getattr(sandbox, "delete", None)
        delete() if callable(delete) else client.delete(sandbox)
        return "deleted", None

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
        await self.in_thread(self._release_sync, handle["client"], handle["sandbox"])

    def _release_sync(self, client, sandbox) -> None:
        """Persist: stop (the id stays recorded). Else delete, then forget the id —
        the record is cleared only after the delete succeeded."""
        if self._persist():
            stop = getattr(sandbox, "stop", None)
            stop() if callable(stop) else client.stop(sandbox)
            state = self.load_state()
            if state.get("sandbox_id"):  # 098 CL-15: the boot sweep has nothing to do
                self.save_state(dict(state, phase="stopped"))
            return
        delete = getattr(sandbox, "delete", None)
        delete() if callable(delete) else client.delete(sandbox)
        self.clear_state()


def _not_found(e: BaseException) -> bool:
    code = getattr(e, "status_code", None) or getattr(e, "status", None)
    return code == 404 or "not found" in str(e).lower()


def shell_executor(*, session_id: str, workspace_dir: str = "", logs_dir: str = ""):
    """``SHELL_BACKEND=daytona``: the session's Daytona sandbox behind the remote executor."""
    from tools.shell.remote_executor import backend_shell_executor
    return backend_shell_executor(DaytonaBackend(session_id=session_id, dev_mode=True),
                                  kind="daytona", session_id=session_id,
                                  default_cwd=DaytonaBackend.workdir)


__all__ = ["DaytonaBackend", "shell_executor"]
