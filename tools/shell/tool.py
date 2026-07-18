"""The persistent `shell` tool (WS-2). NO ``from __future__ import annotations`` —
the action closure's Pydantic param model is introspected by the Registry (the
agent-upgrades-wave4 / GLM param_model landmine).
"""
import asyncio
import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from tools.shell.state import ShellState
from tools.shell.discipline import background_nudge
from tools.shell.executor import DockerShellExecutor
from tools.shell.process_registry import get_process_registry

_MAX_TIMEOUT_SEC = 120.0
_DEFAULT_TIMEOUT_SEC = 60.0


class ShellRunParams(BaseModel):
    """Parameters for ``shell_run`` (plain Pydantic — native-schema safe)."""

    command: str = Field(..., description="Shell command to run (bash). cwd/env persist across calls.")
    background: bool = Field(
        False,
        description="Run detached as a managed job (for servers / long jobs). Returns a "
                    "job id; use the `process` tool to poll its log / status.",
    )
    timeout: Optional[float] = Field(
        None, description="Foreground wall-clock seconds (clamped; ignored for background)."
    )


class ShellTool(BaseTool):
    """Run shell commands in the session's persistent sandbox with cwd/env persistence.

    Posture-gated: reachable only when ``compute_posture_allows(ctx, 1)`` (owner tenant,
    not leaf/sub-agent, not a forged turn) at AGENT_COMPUTE_POSTURE>=1. Every command
    runs INSIDE the session's persistent docker container.
    """

    def __init__(self, name: str = "shell", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._registry = get_process_registry()
        self._states: dict = {}         # session_id -> ShellState
        self._lock = asyncio.Lock()

    @staticmethod
    def _allowed(execution_context) -> bool:
        try:
            from core.config_policy import compute_posture_allows
            return bool(compute_posture_allows(execution_context, 1))
        except Exception:
            return False

    def _state_for(self, session_id: str) -> ShellState:
        st = self._states.get(session_id)
        if st is None:
            st = ShellState()
            self._states[session_id] = st
        return st

    async def _resolve_executor(self, execution_context):
        """Resolve a DockerShellExecutor over the session's SHARED persistent dev
        backend (the process-global pool) — the SAME container the `process` tool
        polls, so a background job's pid is meaningful across both tools."""
        from tools.shell.backend_pool import get_shell_backend
        sid = getattr(execution_context, "session_id", None) or "shell"
        backend = await get_shell_backend(sid)
        return DockerShellExecutor(backend)

    async def _loopback_note(self, session_id: str) -> str:
        """A human line mapping the sandbox's published container ports to the host
        loopback URLs the agent can browser/web_fetch (WS-4). '' when none published."""
        try:
            from tools.shell.backend_pool import peek_backend
            backend = peek_backend(session_id)
            if backend is None:
                return ""
            ports = await backend.published_ports()
            if not ports:
                return ""
            lines = [f"  container :{c} -> http://127.0.0.1:{h}/"
                     for c, h in sorted(ports.items())]
            return ("\nIf it binds one of these ports, reach it from web_fetch/browser at:\n"
                    + "\n".join(lines))
        except Exception:
            return ""

    @BaseTool.action(
        "Run a shell command in the persistent sandbox (cwd/env persist across calls; "
        "background=True for servers/long jobs)",
        param_model=ShellRunParams,
    )
    async def shell_run(self, params: ShellRunParams, execution_context=None):
        if not self._allowed(execution_context):
            return ActionResult(error=(
                "shell is not available for this session: it requires the sandbox-dev "
                "compute posture (AGENT_COMPUTE_POSTURE>=1) and an owner-steered, "
                "non-delegated turn."
            ))

        command = (params.command or "").strip()
        if not command:
            return ActionResult(error="empty command")

        nudge = background_nudge(command, background=params.background)
        if nudge:
            return ActionResult(error=nudge)

        sid = getattr(execution_context, "session_id", None) or "shell"
        try:
            executor = await self._resolve_executor(execution_context)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"shell executor resolve failed: {e}")
            return ActionResult(error=f"shell backend unavailable: {e}")

        try:
            if params.background:
                import time
                job = self._registry.create(sid, command, now=time.time())
                state = self._state_for(sid)
                try:
                    await executor.start_background(command, job.id, state)
                except Exception:
                    # never leave a phantom 'running' job the launch didn't actually start
                    self._registry.mark(sid, job.id, "killed", now=time.time())
                    raise
                ports_note = await self._loopback_note(sid)
                return ActionResult(
                    extracted_content=(
                        f"Started background job `{job.id}`: {command}\n"
                        f"Use the `process` tool (poll/log/kill) with this id.{ports_note}"
                    ),
                    include_in_memory=True,
                )

            timeout = params.timeout or _DEFAULT_TIMEOUT_SEC
            timeout = max(1.0, min(float(timeout), _MAX_TIMEOUT_SEC))
            state = self._state_for(sid)
            clean, new_state, rc = await executor.run_foreground(command, state, timeout=timeout)
            self._states[sid] = new_state
            content = clean if clean else "(no output)"
            if rc != 0:
                return ActionResult(error=f"command exited {rc}\n{content}")
            return ActionResult(extracted_content=content)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"shell_run failed: {e}")
            return ActionResult(error=f"shell_run failed: {e}")
