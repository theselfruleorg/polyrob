"""The `process` background-job manager tool (WS-3). NO ``from __future__ import
annotations`` — the action closures' Pydantic param models are Registry-introspected.
"""
import logging
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from tools.shell.process_registry import get_process_registry


class ProcessListParams(BaseModel):
    pass


class ProcessJobParams(BaseModel):
    job_id: str = Field(..., description="The background job id (from shell_run background=True).")


class ProcessLogParams(BaseModel):
    job_id: str = Field(..., description="The background job id.")
    max_bytes: Optional[int] = Field(
        None, description="Max bytes of the log tail to return (default/capped by the tool)."
    )


class ProcessTool(BaseTool):
    """Manage background shell jobs (list/poll/log/kill).

    Same posture gate as the `shell` tool (compute_posture_allows(ctx, 1)); operates
    only over jobs the CURRENT session started (registry is session-keyed).
    """

    def __init__(self, name: str = "process", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._registry = get_process_registry()

    @staticmethod
    def _allowed(execution_context) -> bool:
        try:
            from core.config_policy import compute_posture_allows
            return bool(compute_posture_allows(execution_context, 1))
        except Exception:
            return False

    def _deny(self) -> ActionResult:
        return ActionResult(error=(
            "process is not available for this session: it requires the sandbox-dev "
            "compute posture (AGENT_COMPUTE_POSTURE>=1) and an owner-steered, "
            "non-delegated turn."
        ))

    async def _executor(self, execution_context):
        """Resolve an executor over the session's SHARED persistent backend (the same
        container the `shell` tool launched the job in — pids are per-container)."""
        from tools.shell.backend_pool import get_shell_backend
        from tools.shell.executor import DockerShellExecutor
        sid = getattr(execution_context, "session_id", None) or "shell"
        backend = await get_shell_backend(sid)
        return DockerShellExecutor(backend)

    @staticmethod
    def _sid(execution_context) -> str:
        return getattr(execution_context, "session_id", None) or "shell"

    @BaseTool.action("List this session's background shell jobs.", param_model=ProcessListParams)
    async def process_list(self, params: ProcessListParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        import time
        jobs = self._registry.list(self._sid(execution_context), now=time.time())
        if not jobs:
            return ActionResult(extracted_content="No background jobs.", include_in_memory=True)
        lines = [f"- `{j.id}` [{j.status}] {j.command[:80]}" for j in jobs]
        return ActionResult(extracted_content="Background jobs:\n" + "\n".join(lines),
                            include_in_memory=True)

    @BaseTool.action("Poll a background job's status (running/done).", param_model=ProcessJobParams)
    async def process_poll(self, params: ProcessJobParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        import time
        sid = self._sid(execution_context)
        job = self._registry.get(sid, params.job_id)
        if job is None:
            return ActionResult(error=f"no such job `{params.job_id}` in this session")
        try:
            executor = await self._executor(execution_context)
            status = await executor.poll(params.job_id)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_poll failed: {e}")
            return ActionResult(error=f"process_poll failed: {e}")
        self._registry.mark(sid, params.job_id, status, now=time.time())
        return ActionResult(extracted_content=f"job `{params.job_id}`: {status}",
                            include_in_memory=True)

    @BaseTool.action("Read a background job's captured log (tail).", param_model=ProcessLogParams)
    async def process_log(self, params: ProcessLogParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        job = self._registry.get(self._sid(execution_context), params.job_id)
        if job is None:
            return ActionResult(error=f"no such job `{params.job_id}` in this session")
        try:
            executor = await self._executor(execution_context)
            log = await executor.read_log(params.job_id, max_bytes=params.max_bytes or 100_000)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_log failed: {e}")
            return ActionResult(error=f"process_log failed: {e}")
        return ActionResult(extracted_content=log or "(no output yet)", include_in_memory=True)

    @BaseTool.action("Kill a background job (tree-kill its process group).", param_model=ProcessJobParams)
    async def process_kill(self, params: ProcessJobParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        import time
        sid = self._sid(execution_context)
        job = self._registry.get(sid, params.job_id)
        if job is None:
            return ActionResult(error=f"no such job `{params.job_id}` in this session")
        try:
            executor = await self._executor(execution_context)
            killed = await executor.kill(params.job_id)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_kill failed: {e}")
            return ActionResult(error=f"process_kill failed: {e}")
        self._registry.mark(sid, params.job_id, "killed", now=time.time())
        msg = f"killed `{params.job_id}`" if killed else f"job `{params.job_id}` had no live pid"
        return ActionResult(extracted_content=msg, include_in_memory=True)
