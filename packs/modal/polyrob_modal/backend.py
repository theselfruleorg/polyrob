"""The Modal execution backend (073 W7): a Modal Sandbox (gVisor) per session.

- SDK: ``modal`` — the ``modal`` extra, imported only in ``setup()``.
- Credentials: ``MODAL_TOKEN_ID`` + ``MODAL_TOKEN_SECRET`` authenticate the client
  in THIS process (else the SDK's own ``~/.modal.toml``). They are never
  forwarded into the sandbox.
- Image: ``MODAL_SANDBOX_IMAGE`` (default ``python:3.12-slim``, from a registry).
- Lifetime: ``MODAL_SANDBOX_TIMEOUT_SEC`` (default 3600) — Modal's own hard cap.
- Persistence: ``MODAL_SANDBOX_SNAPSHOT`` (default OFF). On teardown of a session
  sandbox the filesystem is snapshotted and the image id kept locally; the next
  setup for the same session starts from that image (files and installs survive a
  restart; running processes do not).
- Network: the docker rule (``CODE_EXEC_NETWORK``; a dev sandbox is networked) —
  ``block_network=True`` otherwise.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from core.env import bool_env, int_env
from tools.code_exec.backends.remote_sandbox import RemoteSandboxBackend, env_value, require_sdk

logger = logging.getLogger(__name__)

APP_NAME = "polyrob-sandbox"


class ModalBackend(RemoteSandboxBackend):
    name = "modal"
    isolation = "gvisor"
    extra = "modal"
    workdir = "/workspace"

    def _sdk(self):
        return require_sdk("modal", extra="modal", what="the modal execution backend")

    def _client(self, modal) -> Optional[Any]:
        token_id = env_value("MODAL_TOKEN_ID")
        token_secret = env_value("MODAL_TOKEN_SECRET")
        if token_id and token_secret:
            return modal.Client.from_credentials(token_id, token_secret)
        return None

    def _image(self, modal):
        snap = self.load_state().get("image_id") if bool_env("MODAL_SANDBOX_SNAPSHOT", False) else None
        if snap:
            return modal.Image.from_id(snap)
        return modal.Image.from_registry(os.getenv("MODAL_SANDBOX_IMAGE") or "python:3.12-slim")

    async def _provision(self) -> Any:
        modal = self._sdk()

        def _create():
            client = self._client(modal)
            extra: Dict[str, Any] = {"client": client} if client is not None else {}
            app = modal.App.lookup(APP_NAME, create_if_missing=True, **extra)
            sb = modal.Sandbox.create(
                "sleep", "infinity", app=app, image=self._image(modal),
                timeout=int_env("MODAL_SANDBOX_TIMEOUT_SEC", 3600),
                block_network=not self.network_enabled(), **extra)
            proc = sb.exec("mkdir", "-p", self.workdir)
            proc.wait()
            return sb

        return await self.in_thread(_create)

    async def _exec(self, argv: List[str], *, timeout: float, stdin: Optional[str],
                    env: Dict[str, str]) -> Tuple[int, str, str]:
        sb = self._handle

        def _run():
            kwargs: Dict[str, Any] = {"timeout": int(timeout), "workdir": self.workdir}
            if env:
                argv_env = ["env", *[f"{k}={v}" for k, v in env.items()], *argv]
            else:
                argv_env = argv
            proc = sb.exec(*argv_env, **kwargs)
            if stdin:
                proc.stdin.write(stdin.encode())
                proc.stdin.write_eof()
                proc.stdin.drain()
            proc.wait()
            return proc.returncode, proc.stdout.read(), proc.stderr.read()

        rc, out, err = await self.in_thread(_run)
        return (rc if rc is not None else 1), _text(out), _text(err)

    async def _release(self, handle: Any) -> None:
        def _stop():
            if self.persistent and bool_env("MODAL_SANDBOX_SNAPSHOT", False):
                try:
                    image = handle.snapshot_filesystem()
                    image_id = getattr(image, "object_id", None)
                    if image_id:
                        self.save_state({"image_id": image_id})
                except Exception:
                    logger.warning("modal backend: snapshot failed; the next session "
                                   "starts from the base image", exc_info=True)
            handle.terminate()

        await self.in_thread(_stop)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def shell_executor(*, session_id: str, workspace_dir: str = "", logs_dir: str = ""):
    """``SHELL_BACKEND=modal``: the session's Modal sandbox behind the remote executor."""
    from tools.shell.remote_executor import backend_shell_executor
    return backend_shell_executor(ModalBackend(session_id=session_id, dev_mode=True),
                                  kind="modal", session_id=session_id,
                                  default_cwd=ModalBackend.workdir)


__all__ = ["ModalBackend", "shell_executor"]
