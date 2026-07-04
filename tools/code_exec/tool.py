"""``code_execution`` tool (Item 3 — WS-C3).

One action, ``run_code(language, code, stdin?, timeout?)``, that resolves the
configured ``ExecutionBackend`` and returns an ``ActionResult``. Output is already
capped by the backend; failures (non-zero exit / timeout) surface as ``error``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.code_exec import resolve_backend
from tools.code_exec.result import ExecutionRequest


class RunCodeParams(BaseModel):
    """Parameters for the ``run_code`` action (plain Pydantic — native-schema safe)."""

    language: str = Field(..., description="Language to run: 'python' or 'bash'")
    code: str = Field(..., description="Source code to execute")
    stdin: Optional[str] = Field(None, description="Optional stdin fed to the program")
    timeout: Optional[float] = Field(
        None, description="Wall-clock seconds (clamped to CODE_EXEC_MAX_TIMEOUT_SEC)"
    )


class CodeExecutionTool(BaseTool):
    """Runs code through a pluggable execution backend (default: local subprocess)."""

    def __init__(self, name: str = "code_execution", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._backend = None
        # P1-B F7b: PERSISTENT backends are session-scoped — never share ONE
        # persistent DockerBackend (bound to one session's container) across
        # sessions the way `self._backend` caches the ephemeral one. Keyed by
        # session_id; the lock guards create-once-under-races per tool instance
        # (setup() is rare/first-call-only, so coarse-grained is fine).
        self._persistent_backends: dict = {}
        self._persistent_lock = asyncio.Lock()

    async def _get_backend(self, execution_context=None):
        """Resolve the backend to run on.

        PERSISTENT (opt-in, P1-B F7b): when ``CODE_EXEC_DOCKER_PERSISTENT`` is on
        AND ``execution_context`` carries a truthy ``session_id``, resolve via
        ``resolve_backend(session_id=sid)`` and cache it PER SESSION — the
        container is created once (one ``setup()`` call) and reused for every
        later ``run_code`` call in that session.

        EPHEMERAL (default, byte-for-byte unchanged): flag off, or no
        session_id — one process-wide, session-less backend cached on
        ``self._backend``, exactly as before this change.
        """
        from tools.code_exec import code_exec_docker_persistent_enabled

        sid = None
        if code_exec_docker_persistent_enabled():
            sid = getattr(execution_context, "session_id", None) or None

        if sid:
            cached = self._persistent_backends.get(sid)
            if cached is not None:
                return cached
            async with self._persistent_lock:
                cached = self._persistent_backends.get(sid)  # re-check: lost the race?
                if cached is None:
                    cached = resolve_backend(session_id=sid)
                    await cached.setup()
                    self._persistent_backends[sid] = cached
                return cached

        if self._backend is None:
            backend = resolve_backend()
            await backend.setup()
            self._backend = backend
        return self._backend

    def _resolve_workdir(self, execution_context) -> Optional[str]:
        """Run inside the session workspace when a session is resolvable, else tempdir."""
        try:
            sid = getattr(execution_context, "session_id", None) or getattr(self, "session_id", None)
            if sid:
                from agents.task.path import pm
                return str(pm().get_workspace_dir(sid))
        except Exception:
            pass
        return None

    @staticmethod
    def _to_action_result(result):
        from tools.controller.types import ActionResult

        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        if result.truncated:
            parts.append("[output truncated]")
        content = "\n".join(parts) if parts else "(no output)"

        if result.timed_out:
            return ActionResult(error=f"code execution timed out after {result.duration_sec:.1f}s\n{content}")
        if result.exit_code not in (0, None):
            return ActionResult(error=f"code exited with status {result.exit_code}\n{content}")
        return ActionResult(extracted_content=content)

    @BaseTool.action(
        "Execute python or bash code in a local subprocess (timeout + output cap + env allowlist)",
        param_model=RunCodeParams,
    )
    async def run_code(self, params: RunCodeParams, execution_context=None):
        """Execute ``params.code`` via the configured backend; return an ActionResult."""
        from tools.code_exec.sandbox_guard import code_exec_execution_blocked_reason
        blocked = code_exec_execution_blocked_reason()
        if blocked:
            from tools.controller.types import ActionResult
            return ActionResult(error=blocked)
        try:
            backend = await self._get_backend(execution_context)
            req = ExecutionRequest(
                language=params.language,
                code=params.code,
                stdin=params.stdin,
                timeout=params.timeout,
                workdir=self._resolve_workdir(execution_context),
            )
            result = await backend.run(req)
            return self._to_action_result(result)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"run_code failed: {e}")
            from tools.controller.types import ActionResult
            return ActionResult(error=f"run_code failed: {e}")
