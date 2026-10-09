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


class ProcessWaitParams(BaseModel):
    job_id: str = Field(..., description="The background job id.")
    timeout: Optional[float] = Field(
        None, description="Max seconds to wait for the job to finish (default 60; "
                          "capped at the shell foreground ceiling).",
    )


class ProcessWriteParams(BaseModel):
    job_id: str = Field(..., description="The background job id.")
    data: str = Field(..., description="Text to send to the job's stdin as-is (no newline "
                                       "added; use process_submit for a line).")


class ProcessSubmitParams(BaseModel):
    job_id: str = Field(..., description="The background job id.")
    data: str = Field("", description="Text to send to the job's stdin, followed by a "
                                      "newline (empty = just Enter).")


class ProcessLogParams(BaseModel):
    job_id: str = Field(..., description="The background job id.")
    max_bytes: Optional[int] = Field(
        None, description="Max bytes of the log tail to return (default/capped by the tool)."
    )


class ProcessTool(BaseTool):
    """Manage background shell jobs (list/poll/wait/log/kill/write/submit/close).

    Same posture gate as the `shell` tool (compute_posture_allows(ctx, 1)); operates
    only over jobs the CURRENT session started (registry is session-keyed). 073 W4:
    a job the in-memory registry lost (a restart) is still reported from its
    durable receipt (``tools/shell/receipts.py``, the session's own data dir).
    """

    def __init__(self, name: str = "process", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._registry = get_process_registry()

    @staticmethod
    def _allowed(execution_context) -> bool:
        from core.config_policy import compute_posture_allows_safe
        return compute_posture_allows_safe(execution_context, 1)

    def _deny(self) -> ActionResult:
        return ActionResult(error=(
            "process is not available for this session: it requires the sandbox-dev "
            "compute posture (AGENT_COMPUTE_POSTURE>=1) and an owner-steered, "
            "non-delegated turn."
        ))

    async def _executor(self, execution_context, job=None):
        """Resolve the executor the job was launched by: the session's SHARED
        persistent container (pids are per-container), or — for a posture-3 host
        job — the host executor, which re-runs the host gate for THIS turn."""
        from tools.shell.backend_pool import resolve_shell_executor
        return await resolve_shell_executor(
            execution_context, kind=getattr(job, "backend", None) or "docker")

    async def _status_line(self, executor, job_id: str, status: str) -> str:
        if status != "done":
            return f"job `{job_id}`: {status}"
        rc = None
        try:
            rc = await executor.exit_code(job_id)
        except Exception:
            rc = None
        if rc is None:
            return f"job `{job_id}`: done"
        from tools.shell.output import exit_note
        note = exit_note(rc)
        return f"job `{job_id}`: done (exit {rc}{' — ' + note if note else ''})"

    @staticmethod
    def _sid(execution_context) -> str:
        return getattr(execution_context, "session_id", None) or "shell"

    @staticmethod
    def _uid(execution_context):
        return getattr(execution_context, "user_id", None)

    def _receipt(self, execution_context, job_id: str):
        from tools.shell.receipts import load_receipt
        try:
            return load_receipt(self._sid(execution_context), self._uid(execution_context), job_id)
        except Exception:
            return None

    async def _finalize(self, executor, job, execution_context, *, status=None,
                        overwrite: bool = False) -> None:
        """Write the job's durable receipt once it is seen finished (fail-open)."""
        from tools.shell.receipts import finalize_job
        try:
            await finalize_job(executor, job, session_id=self._sid(execution_context),
                               user_id=self._uid(execution_context), registry=self._registry,
                               status=status, overwrite=overwrite)
        except Exception:
            getattr(self, "logger", logging.getLogger(__name__)).debug(
                "process: receipt write failed", exc_info=True)

    def _no_job(self, execution_context, job_id: str):
        """The answer for an id the live registry does not hold: its receipt, or
        a plain 'no such job'."""
        rec = self._receipt(execution_context, job_id)
        if rec is None:
            return None, ActionResult(error=f"no such job `{job_id}` in this session")
        return rec, None

    async def _stdin_target(self, execution_context, job_id: str):
        """``(executor, job, None)`` for a LIVE job, else ``(None, None, error)``."""
        job = self._registry.get(self._sid(execution_context), job_id)
        if job is None:
            rec = self._receipt(execution_context, job_id)
            if rec is not None:
                return None, None, ActionResult(error=f"job `{job_id}` already finished; its stdin is gone")
            return None, None, ActionResult(error=f"no such job `{job_id}` in this session")
        executor = await self._executor(execution_context, job)
        if not (hasattr(executor, "write_stdin") and hasattr(executor, "close_stdin")):
            return None, None, ActionResult(error=(
                f"job `{job_id}` runs on the {getattr(executor, 'kind', '?')} shell backend, "
                "which has no stdin support"))
        status = await executor.poll(job_id)
        if status != "running":
            await self._finalize(executor, job, execution_context)
            return None, None, ActionResult(error=f"job `{job_id}` is not running ({status}); "
                                                  "its stdin is gone")
        return executor, job, None

    async def _send(self, execution_context, job_id: str, data: str):
        from tools.shell.jobs import STDIN_MAX_BYTES, ends_with_password_prompt
        raw = (data or "").encode("utf-8")
        # 073 W2: a job may be a shell, so typed text meets the same floor as
        # shell_run (the controller pre-hook adds the owner's deny list).
        from core.security.command_guard import classify
        verdict = classify(data or "") if (data or "").strip() else None
        if verdict is not None and verdict.is_floor:
            return ActionResult(error=(
                f"refused: this input {verdict.reason}. It is on the shell guard's floor "
                "and no approval can allow it. Do not retry it in another form."))
        if len(raw) > STDIN_MAX_BYTES:
            return ActionResult(error=f"input is limited to {STDIN_MAX_BYTES} bytes per call")
        try:
            executor, job, err = await self._stdin_target(execution_context, job_id)
            if err is not None:
                return err
            # cross-agent parity: the agent never types a secret into a prompt.
            tail = await executor.read_log(job_id, max_bytes=512)
            if ends_with_password_prompt(tail):
                return ActionResult(error=(
                    f"refused: job `{job_id}` is waiting at a password/secret prompt. The "
                    "agent never types a password. Ask the owner to do this step, or kill "
                    "the job."
                ))
            ok, msg = await executor.write_stdin(job_id, raw)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process stdin write failed: {e}")
            return ActionResult(error=f"process stdin write failed: {e}")
        if not ok:
            return ActionResult(error=f"job `{job_id}`: {msg}")
        return ActionResult(extracted_content=f"job `{job_id}`: {msg}. Use process_log or "
                                              "process_wait to read the response.",
                            include_in_memory=True)

    @BaseTool.action("List this session's background shell jobs.", param_model=ProcessListParams)
    async def process_list(self, params: ProcessListParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        import time
        jobs = self._registry.list(self._sid(execution_context), now=time.time())
        lines = [f"- `{j.id}` [{j.status}]{' (host)' if j.backend == 'host' else ''}"
                 f"{' (pty)' if getattr(j, 'pty', False) else ''} {j.command[:80]}"
                 for j in jobs]
        live = {j.id for j in jobs}
        try:
            from tools.shell.receipts import list_receipts
            receipts = [r for r in list_receipts(self._sid(execution_context),
                                                 self._uid(execution_context))
                        if r.get("id") not in live]
        except Exception:
            receipts = []
        for r in receipts[-20:]:
            rc = r.get("exit_code")
            lines.append(f"- `{r.get('id')}` [{r.get('status') or 'done'}"
                         f"{'' if rc is None else f', exit {rc}'}] (receipt)"
                         f"{' (host)' if r.get('backend') == 'host' else ''} "
                         f"{str(r.get('command') or '')[:80]}")
        if not lines:
            return ActionResult(extracted_content="No background jobs.", include_in_memory=True)
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
            rec, err = self._no_job(execution_context, params.job_id)
            if err is not None:
                return err
            from tools.shell.receipts import receipt_status_line
            return ActionResult(extracted_content=receipt_status_line(rec), include_in_memory=True)
        try:
            executor = await self._executor(execution_context, job)
            status = await executor.poll(params.job_id)
            line = await self._status_line(executor, params.job_id, status)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_poll failed: {e}")
            return ActionResult(error=f"process_poll failed: {e}")
        if status != "running":
            await self._finalize(executor, job, execution_context)
        else:
            self._registry.mark(sid, params.job_id, status, now=time.time())
        return ActionResult(extracted_content=line, include_in_memory=True)

    @BaseTool.action("Wait (bounded) for a background job to finish, then report its "
                     "status and log tail.", param_model=ProcessWaitParams)
    async def process_wait(self, params: ProcessWaitParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        import asyncio
        import time
        from tools.code_exec.limits import dev_exec_max_timeout_sec
        sid = self._sid(execution_context)
        job = self._registry.get(sid, params.job_id)
        if job is None:
            rec, err = self._no_job(execution_context, params.job_id)
            if err is not None:
                return err
            from tools.shell.output import shape_output
            from tools.shell.receipts import receipt_status_line
            tail_text, _ = shape_output(str(rec.get("log_tail") or "")[-4000:], max_chars=4000)
            return ActionResult(extracted_content=(f"{receipt_status_line(rec)}\n--- log tail ---\n"
                                                   f"{tail_text or '(no output)'}"),
                                include_in_memory=True)
        budget = max(1.0, min(float(params.timeout or 60.0), dev_exec_max_timeout_sec()))
        try:
            executor = await self._executor(execution_context, job)
            deadline = time.monotonic() + budget
            status = await executor.poll(params.job_id)
            delay = 0.25
            while status == "running" and time.monotonic() < deadline:
                await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2, 5.0)
                status = await executor.poll(params.job_id)
            line = await self._status_line(executor, params.job_id, status)
            tail = await executor.read_log(params.job_id, max_bytes=4000)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_wait failed: {e}")
            return ActionResult(error=f"process_wait failed: {e}")
        if status != "running":
            await self._finalize(executor, job, execution_context)
        else:
            self._registry.mark(sid, params.job_id, status, now=time.time())
        if status == "running":
            line += f" (still running after {budget:g}s)"
        from tools.shell.output import shape_output
        tail_text, _ = shape_output(tail or "", max_chars=4000)
        return ActionResult(extracted_content=f"{line}\n--- log tail ---\n{tail_text or '(no output yet)'}",
                            include_in_memory=True)

    @BaseTool.action("Read a background job's captured log (tail).", param_model=ProcessLogParams)
    async def process_log(self, params: ProcessLogParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        job = self._registry.get(self._sid(execution_context), params.job_id)
        if job is None:
            rec, err = self._no_job(execution_context, params.job_id)
            if err is not None:
                return err
            from tools.shell.output import shape_output
            log = str(rec.get("log_tail") or "")
            if params.max_bytes:
                log = log[-max(1, int(params.max_bytes)):]
            text, _ = shape_output(log)
            return ActionResult(extracted_content=(f"(from the durable receipt of `{params.job_id}`)\n"
                                                   f"{text or '(no output)'}"),
                                include_in_memory=True)
        try:
            executor = await self._executor(execution_context, job)
            log = await executor.read_log(params.job_id, max_bytes=params.max_bytes or 100_000)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_log failed: {e}")
            return ActionResult(error=f"process_log failed: {e}")
        from tools.shell.output import shape_output
        text, _ = shape_output(log or "")
        return ActionResult(extracted_content=text or "(no output yet)", include_in_memory=True)

    @BaseTool.action("Kill a background job (tree-kill its process group).", param_model=ProcessJobParams)
    async def process_kill(self, params: ProcessJobParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        import time
        sid = self._sid(execution_context)
        job = self._registry.get(sid, params.job_id)
        if job is None:
            rec, err = self._no_job(execution_context, params.job_id)
            if err is not None:
                return err
            return ActionResult(extracted_content=(f"job `{params.job_id}` already finished "
                                                   "(durable receipt); nothing to kill"),
                                include_in_memory=True)
        try:
            executor = await self._executor(execution_context, job)
            was_running = (await executor.poll(params.job_id)) == "running"
            killed = await executor.kill(params.job_id)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_kill failed: {e}")
            return ActionResult(error=f"process_kill failed: {e}")
        if was_running:
            self._registry.mark(sid, params.job_id, "killed", now=time.time())
            await self._finalize(executor, job, execution_context, status="killed", overwrite=True)
        else:
            await self._finalize(executor, job, execution_context)
        msg = f"killed `{params.job_id}`" if killed else f"job `{params.job_id}` had no live pid"
        return ActionResult(extracted_content=msg, include_in_memory=True)

    @BaseTool.action("Send text to a running background job's stdin (no newline added).",
                     param_model=ProcessWriteParams)
    async def process_write(self, params: ProcessWriteParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        if not params.data:
            return ActionResult(error="data is empty (use process_submit for a bare Enter)")
        return await self._send(execution_context, params.job_id, params.data)

    @BaseTool.action("Send a line (text + newline) to a running background job's stdin.",
                     param_model=ProcessSubmitParams)
    async def process_submit(self, params: ProcessSubmitParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        return await self._send(execution_context, params.job_id, (params.data or "") + "\n")

    @BaseTool.action("Close a running background job's stdin (EOF; Ctrl-D on a pty job).",
                     param_model=ProcessJobParams)
    async def process_close(self, params: ProcessJobParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        try:
            executor, job, err = await self._stdin_target(execution_context, params.job_id)
            if err is not None:
                return err
            ok, msg = await executor.close_stdin(params.job_id)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"process_close failed: {e}")
            return ActionResult(error=f"process_close failed: {e}")
        if not ok:
            return ActionResult(error=f"job `{params.job_id}`: {msg}")
        return ActionResult(extracted_content=f"job `{params.job_id}`: {msg}", include_in_memory=True)
