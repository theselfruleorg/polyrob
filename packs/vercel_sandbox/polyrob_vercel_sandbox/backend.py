"""The Vercel Sandbox execution backend (073 W7): a Vercel Sandbox microVM per session.

- SDK: ``vercel`` (``vercel.sandbox.Sandbox``) — the ``vercel-sandbox`` extra,
  imported only in ``setup()``.
- Credentials: ``VERCEL_TOKEN`` + ``VERCEL_TEAM_ID`` + ``VERCEL_PROJECT_ID`` (or the
  SDK's own ``VERCEL_OIDC_TOKEN``) authenticate the client in THIS process; never
  forwarded into the sandbox.
- Runtime: ``VERCEL_SANDBOX_RUNTIME`` (default ``python3.13``).
- Lifetime: ``VERCEL_SANDBOX_TIMEOUT_SEC`` (default 2700 — the provider caps it).
- Persistence: none. A Vercel sandbox is ephemeral by the provider's design; a
  session's files live until the sandbox stops (teardown or the provider's
  timeout). There is no stop/resume to wire.
- Network: Vercel gives no per-sandbox network deny, so ``capabilities["network"]``
  is always True (``can_block_network = False``).
- Paths: the default user cannot write ``/``, so the shell's workspace is the
  provider's ``/vercel/sandbox`` and its job files ``/tmp/polyrob-jobs/<sid>``.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from core.env import int_env
from tools.code_exec.backends.remote_sandbox import RemoteSandboxBackend, env_value, require_sdk

logger = logging.getLogger(__name__)


class VercelSandboxBackend(RemoteSandboxBackend):
    name = "vercel_sandbox"
    isolation = "microvm"
    extra = "vercel-sandbox"
    workdir = "/vercel/sandbox"
    can_block_network = False

    def _sdk(self):
        return require_sdk("vercel.sandbox", extra="vercel-sandbox",
                           what="the vercel_sandbox execution backend")

    def _auth(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, env in (("token", "VERCEL_TOKEN"), ("team_id", "VERCEL_TEAM_ID"),
                         ("project_id", "VERCEL_PROJECT_ID")):
            value = env_value(env)
            if value:
                out[key] = value
        return out

    async def _provision(self) -> Any:
        sdk = self._sdk()

        def _create():
            return sdk.Sandbox.create(
                runtime=env_value("VERCEL_SANDBOX_RUNTIME") or "python3.13",
                timeout=int_env("VERCEL_SANDBOX_TIMEOUT_SEC", 2700) * 1000,
                **self._auth())

        return await self.in_thread(_create)

    async def _exec(self, argv: List[str], *, timeout: float, stdin: Optional[str],
                    env: Dict[str, str]) -> Tuple[int, str, str]:
        sandbox = self._handle
        if stdin:
            # run_command takes no stdin stream: feed it through printf.
            inner = f"printf '%s' {self.shell_join([stdin])} | {self.shell_join(argv)}"
            argv = ["bash", "-c", inner]

        def _run():
            kwargs: Dict[str, Any] = {"cwd": self.workdir}
            if env:
                kwargs["env"] = dict(env)
            done = sandbox.run_command(argv[0], list(argv[1:]), **kwargs)
            return done.exit_code, _call(done, "stdout"), _call(done, "stderr")

        rc, out, err = await self.in_thread(_run)
        return (rc if rc is not None else 1), out, err

    async def _release(self, handle: Any) -> None:
        await self.in_thread(handle.stop)


def _call(obj, attr: str) -> str:
    value = getattr(obj, attr, "")
    if callable(value):
        value = value()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def shell_executor(*, session_id: str, workspace_dir: str = "", logs_dir: str = ""):
    """``SHELL_BACKEND=vercel_sandbox``: the session's sandbox behind the remote executor."""
    from tools.shell.remote_executor import backend_shell_executor
    return backend_shell_executor(VercelSandboxBackend(session_id=session_id, dev_mode=True),
                                  kind="vercel_sandbox", session_id=session_id,
                                  default_cwd=VercelSandboxBackend.workdir)


__all__ = ["VercelSandboxBackend", "shell_executor"]
