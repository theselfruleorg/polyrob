"""`cronjob` agent tool (roadmap P5 / Reference §30).

Lets an agent schedule durable, recurring or one-shot work that outlives the
current turn — the home for tasks `delegate_task` explicitly cannot do. Thin glue
over :class:`cron.service.CronService`; all scheduling logic lives there.

Off by default: the tool is only registered when ``CRON_ENABLED=true`` and is not
in the default ``tool_ids`` list, so production (``UVICORN_WORKERS=1``) is
unaffected until cron is explicitly turned on.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from core.env import bool_env as _bool_env

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from cron.jobs import CronJobStore
from cron.schedule import ScheduleError
from cron.service import CronService


# --- param models ------------------------------------------------------------

class CronScheduleAction(BaseModel):
    """Schedule a durable task. NOT bound to this turn — it runs later/on a cycle."""
    model_config = ConfigDict(extra="forbid")
    task: str = Field(..., description="The task the scheduled agent should perform.", min_length=10)
    schedule: str = Field(..., description="When to run: '30m'/'2h'/'1d', 'every monday 09:00', "
                                           "5-field cron '*/15 * * * *', or an ISO timestamp (one-shot).")
    max_duration_seconds: int = Field(default=180, le=180,
                                      description="Hard cap per run (<=180s, the cron 3-minute limit).")
    deliver: Optional[str] = Field(default=None,
                                   description="Optional out-of-band delivery sink for the result: "
                                               "'telegram', 'email', or 'twitter'. Omit to keep silent.")
    deliver_target: Optional[str] = Field(default=None,
                                          description="Optional explicit recipient (your own email / chat id). "
                                                      "Omit to deliver to your own configured channel.")
    wake_agent: bool = Field(default=True,
                             description="If false, this tick runs without invoking the LLM "
                                         "(a $0 no-op tick). Default true = normal agent run.")


class CronListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CronCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(..., description="The id of the cron job to cancel.")


class CronJobTool(BaseTool):
    """Agent tool exposing schedule/list/cancel over the cron job store."""

    def _resolve_service(self) -> CronService:
        if getattr(self, "_cron_service", None) is None:
            data_dir = getattr(self.config, "data_dir", "data") if getattr(self, "config", None) else "data"
            self._cron_service = CronService(CronJobStore(os.path.join(data_dir, "cron.db")))
        return self._cron_service

    @staticmethod
    def _user(execution_context) -> str:
        uid = getattr(execution_context, "user_id", None)
        if uid:
            return uid
        from core.identity import resolve_identity
        return resolve_identity()  # owner principal or "local" — never the anon sentinel (ME-D4)

    @BaseTool.action("Schedule a durable task to run later or on a recurring cycle (not bound to this turn).",
                     param_model=CronScheduleAction)
    async def cronjob_schedule(self, params: CronScheduleAction, execution_context=None) -> ActionResult:
        # Carry delivery routing in the free-form payload the runner reads (W3). The
        # action model stays extra="forbid" — these are typed fields, validated here.
        payload = {}
        if params.deliver:
            payload["deliver"] = params.deliver
        if params.deliver_target:
            payload["deliver_target"] = params.deliver_target
        if not params.wake_agent:
            payload["wake_agent"] = False
        try:
            job = self._resolve_service().schedule(
                task=params.task, schedule_spec=params.schedule,
                user_id=self._user(execution_context),
                payload=payload or None,
                max_duration_seconds=params.max_duration_seconds,
            )
        except ScheduleError as e:
            return ActionResult(error=f"Invalid schedule: {e}", include_in_memory=True)
        when = job.next_run_at.isoformat() if job.next_run_at else "?"
        kind = "one-shot" if job.one_shot else "recurring"
        return ActionResult(
            extracted_content=f"Scheduled {kind} cron job `{job.id}` — next run {when}.",
            include_in_memory=True,
        )

    @BaseTool.action("List scheduled cron jobs for the current user.", param_model=CronListAction)
    async def cronjob_list(self, params: CronListAction, execution_context=None) -> ActionResult:
        jobs = self._resolve_service().list_jobs(user_id=self._user(execution_context))
        if not jobs:
            return ActionResult(extracted_content="No scheduled cron jobs.", include_in_memory=True)
        lines = [
            f"- `{j.id}` [{j.status}] {j.schedule_spec} -> "
            f"{j.next_run_at.isoformat() if j.next_run_at else '-'}: {j.task[:60]}"
            for j in jobs
        ]
        return ActionResult(extracted_content="Scheduled cron jobs:\n" + "\n".join(lines),
                            include_in_memory=True)

    @BaseTool.action("Cancel a scheduled cron job by id.", param_model=CronCancelAction)
    async def cronjob_cancel(self, params: CronCancelAction, execution_context=None) -> ActionResult:
        ok = self._resolve_service().cancel(params.job_id, user_id=self._user(execution_context))
        msg = f"Cancelled cron job `{params.job_id}`." if ok else f"No such cron job `{params.job_id}`."
        return ActionResult(extracted_content=msg, include_in_memory=True)


def cron_enabled() -> bool:
    """Whether the cron subsystem is turned on (opt-in; off in production by default)."""
    return _bool_env("CRON_ENABLED", False)


def register_cronjob_tool(force: bool = False) -> bool:
    """Register the 'cronjob' descriptor + class IFF cron is enabled (or forced).

    Delegates to ``register_optional_tool`` (single shared factory). The descriptor
    is inserted idempotently before calling ``register_tool_class``.

    Returns True when registered. No-op (returns False) when ``CRON_ENABLED`` is off,
    so flag-off => ``get_tool_class('cronjob')`` is None and default deploys are
    unaffected. ``cronjob`` is never in the default ``tool_ids`` — agents opt in.
    """
    from tools.descriptors import (
        ToolDescriptor,
        ToolCategory,
        register_optional_tool,
    )

    return register_optional_tool(
        "cronjob",
        CronJobTool,
        ToolDescriptor(
            name="cronjob",
            description="Schedule durable recurring/one-shot agent runs (cronjob_schedule/list/cancel)",
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=80,
            is_optional=True,
        ),
        cron_enabled,
        force=force,
    )
