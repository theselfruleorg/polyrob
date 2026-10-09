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
from typing import Any, Dict, List, Optional

from core.env import bool_env as _bool_env

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from cron.jobs import CronJobStore
from cron.schedule import ScheduleError
from cron.service import AGENT_MAX_ACTIVE_JOBS, CronService


# --- param models ------------------------------------------------------------

class CronScheduleAction(BaseModel):
    """Schedule a durable task. NOT bound to this turn — it runs later/on a cycle."""
    model_config = ConfigDict(extra="forbid")
    task: str = Field(..., description="The task the scheduled agent should perform.", min_length=10)
    schedule: str = Field(..., description="When to run: '30m'/'2h'/'1d', 'every monday 09:00', "
                                           "5-field cron '*/15 * * * *', or an ISO timestamp (one-shot).")
    max_duration_seconds: int = Field(default=600, ge=60, le=1800,
                                      description="Hard cap per run (60-1800s; default 600). A money rail "
                                                  "that reconciles, quotes, gates, swaps and reports needs "
                                                  "~15-25 min at 1-5 min/step — the 600 s ceiling cut the "
                                                  "hourly buyback at step 6 before its swap (2026-09-18).")
    deliver: Optional[str] = Field(default=None,
                                   description="Optional out-of-band delivery sink for the result: "
                                               "'telegram' or 'email' (to the owner), or 'twitter' — a "
                                               "PUBLIC post on the agent's X account, when the X pack "
                                               "is enabled. Omit to keep silent.")
    deliver_target: Optional[str] = Field(default=None,
                                          description="Optional explicit recipient. IGNORED unless the "
                                                      "operator enabled CRON_DELIVERY_ALLOW_EXPLICIT_TARGET; "
                                                      "by default delivery always goes to the owner's own "
                                                      "channel.")
    wake_agent: bool = Field(default=True,
                             description="If false, this tick runs without invoking the LLM "
                                         "(a $0 no-op tick). Default true = normal agent run.")
    rig: Optional[str] = Field(default=None,
                               description="Optional named tool rig for this job's runs: "
                                           "'money_rail', 'social', 'research', 'ops', or 'full' "
                                           "(everything this deploy grants an autonomous run). "
                                           "A narrow rig ships far fewer tool schemas per step, "
                                           "which is most of the cost of a recurring job. A rig "
                                           "is a REQUEST, not a grant: a rig with a tool an "
                                           "agent-created job cannot carry is REFUSED with the "
                                           "rigs you can use. Omit to use this deploy's default.")
    skills: Optional[List[str]] = Field(default=None,
                                        description="Optional skill ids this job's runs must load (its "
                                                    "doctrine, e.g. ['x-engagement']). Pinned skills load "
                                                    "every run instead of depending on keyword matching; "
                                                    "they grant no tool. Up to 8; omit to match by keyword.")
    target_token: Optional[Dict[str, str]] = Field(
        default=None,
        description="For a job that BUYS one specific token: {'chain': ..., 'address': ...}. "
                    "Every run's buys are then refused unless they name exactly this contract "
                    "— the address lives in the job as data, never only in the task text. "
                    "It only restricts; it grants nothing.")
    read_verb: Optional[Dict[str, Any]] = Field(
        default=None,
        description="W9: make this a DETERMINISTIC read job — one allowlisted read verb, "
                    "no LLM turn per tick: {'verb': 'defi_data.pool_metrics', 'params': "
                    "{...the verb's params...}, 'deliver_on': 'alert'|'always'|'never'}. "
                    "'alert' (default) delivers only a result with an ALERT line or an error. "
                    "Verbs: defi_data.pool_metrics, defi_data.price, defi_data.token_info, "
                    "defi_data.wallet_holdings (public reads only). A price/value watch adds "
                    "'alert': {'field': '<key of the verb result metadata>', 'below': n, "
                    "'above': n} — the verb compares, you never do; an unreadable value alerts.")
    write_verb: Optional[Dict[str, Any]] = Field(
        default=None,
        description="OWNER TURN ONLY: make this a deterministic WRITE job — the one allowlisted "
                    "write verb, no LLM turn per tick: {'verb': 'agent_nft.agent_nft_collection_reveal', 'params': "
                    "{'chain': 'robinhood', 'max_ids': 10, 'dry_run': false}, 'deliver_on': "
                    "'alert'}. It SPENDS gas from the treasury (at most AGENT_NFT_REVEAL_MAX_GAS_USD "
                    "per run; no value, no asset) and runs only while CRON_WRITE_JOBS_ENABLED=true. "
                    "Omitting dry_run=false only simulates. May recur every minute.")
    pause_windows: Optional[List[List[Any]]] = Field(
        default=None,
        description="Per-job pause windows: [[start, end], ...] as epoch seconds or ISO-8601 "
                    "UTC. A tick inside a window is a $0 skip (e.g. hold the buyback from "
                    "T-1h to 24h after the mint window).")


class CronListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CronCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(..., description="The id of the cron job to cancel.")


class CronShowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(..., description="The id of the cron job to show in full.")


class CronEditAction(BaseModel):
    """Amend a live job IN PLACE. Pass only the fields to change."""
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(..., description="The id of the cron job to edit.")
    old_text: Optional[str] = Field(default=None,
                                    description="Exact passage of the CURRENT task to replace "
                                                "(copy it from cronjob_show; must occur exactly "
                                                "once). The rest of the task is kept verbatim — "
                                                "never re-type a whole task to change one value.")
    new_text: Optional[str] = Field(default=None,
                                    description="Replacement for old_text ('' deletes the passage).")
    schedule: Optional[str] = Field(default=None,
                                    description="New schedule spec; the next run is recomputed from now.")
    max_duration_seconds: Optional[int] = Field(default=None, ge=60, le=1800,
                                                description="New hard cap per run (60-1800s).")
    rig: Optional[str] = Field(default=None,
                               description="New named tool rig, or 'none' to clear it.")
    deliver: Optional[str] = Field(default=None,
                                   description="New delivery sink ('telegram'|'email' to the owner, "
                                               "'twitter' = a PUBLIC X post), or 'none' to drop it.")
    skills: Optional[List[str]] = Field(default=None,
                                        description="Replace the pinned skill ids ([] clears them).")
    target_token: Optional[Dict[str, str]] = Field(
        default=None,
        description="Replace the declared buy target {'chain', 'address'}; {} clears it.")
    read_verb: Optional[Dict[str, Any]] = Field(
        default=None, description="Replace the job's read verb (see cronjob_schedule); {} clears it.")
    write_verb: Optional[Dict[str, Any]] = Field(
        default=None, description="OWNER TURN ONLY: replace the job's write verb (see "
                                  "cronjob_schedule); {} clears it.")
    pause_windows: Optional[List[List[Any]]] = Field(
        default=None, description="Replace the job's pause windows ([[start, end], ...]); [] clears them.")


_WRITE_VERB_OWNER_ONLY = (
    "write_verb refused: a scheduled write job spends from the treasury with no model turn, "
    "so only a genuine owner turn may create or change one. Ask the owner.")


def _authored_by(job) -> str:
    return str((job.payload or {}).get("authored_by") or "")



def _provider_line(payload: dict) -> str:
    """Which seat a job runs on — and, for a pin that is credit-dead, where it goes instead.

    2026-10-02: an audit run could not confirm DATA PULL was pinned to an
    unfunded z.ai seat, because show named the rig but never the provider.
    """
    pin = payload.get("provider")
    if not pin:
        return "provider: (owner's seat)"
    model = payload.get("model")
    line = f"provider: {pin}{' / ' + model if model else ''} (pinned)"
    try:
        import core.runtime_config as rc
        if rc._sentinel_active(pin):
            live = rc.resolve_live_provider(pin)
            line += (f" — credit-dead now; runs on {live}" if live
                     else " — credit-dead now; nothing can serve, runs are skipped")
    except Exception:
        pass
    return line

class CronJobTool(BaseTool):
    """Agent tool exposing schedule/list/show/edit/cancel over the cron job store."""

    def _resolve_service(self) -> CronService:
        if getattr(self, "_cron_service", None) is None:
            from core.runtime_paths import data_dir_or_home
            data_dir = data_dir_or_home(
                getattr(self.config, "data_dir", None) if getattr(self, "config", None) else None)
            self._cron_service = CronService(CronJobStore(os.path.join(data_dir, "cron.db")))
        return self._cron_service

    @staticmethod
    def _user(execution_context) -> str:
        uid = getattr(execution_context, "user_id", None)
        if uid:
            return uid
        from core.identity import resolve_identity
        return resolve_identity()  # owner principal or "local" — never the anon sentinel (ME-D4)

    @staticmethod
    def _self_scheduling_refusal(execution_context) -> Optional[str]:
        try:
            from tools.controller.turn_origin import _is_forged_or_autonomous_turn
            forged = bool(_is_forged_or_autonomous_turn(execution_context, None))
        except Exception:
            forged = True
        if not forged:
            return None
        # M07 re-keyed on the regime: an autonomous goal/cron run may schedule
        # its own recurring work under AUTONOMY_MODE=autonomous. Forged, leaf,
        # room and (via the correspondent gate) tainted turns never may.
        from tools.goal_tools import autonomous_run_may_schedule
        if autonomous_run_may_schedule(execution_context):
            return None
        from core.security.refusals import record_refusal
        try:
            record_refusal("forged_turn", tool="cronjob_schedule",
                           user_id=getattr(execution_context, "user_id", None) or "",
                           session_id=getattr(execution_context, "session_id", None) or "")
        except Exception:
            pass
        return ("cronjob_schedule denied: only a genuine owner turn may start a "
                "recurring job — it outlives this run and its budget. This turn is "
                "autonomous, delegated, or a self-wake re-entry. Record a goal "
                "(goal_create) or ask the owner to schedule it.")

    @BaseTool.action("Schedule a durable task to run later or on a recurring cycle (not bound to this turn).",
                     param_model=CronScheduleAction)
    async def cronjob_schedule(self, params: CronScheduleAction, execution_context=None) -> ActionResult:
        # M07 (security analysis 2026-09-23): a scheduled job OUTLIVES the run
        # that creates it and its budget. A forged re-entry, a sub-agent/leaf,
        # a room turn may not start one. An autonomous goal/cron run may only
        # under the autonomous/armed regime (core/config_policy/money_regime.py).
        # Fail-closed on a probe error.
        refusal = self._self_scheduling_refusal(execution_context)
        if refusal:
            return ActionResult(error=refusal, include_in_memory=True)
        # One rule (tools.goal_tools.owner_authored_turn): a genuine owner turn
        # that has read no third-party content authors the job; else the agent.
        from tools.goal_tools import owner_authored_turn
        owner_turn = owner_authored_turn(execution_context)
        # Carry delivery routing in the free-form payload the runner reads (W3). The
        # action model stays extra="forbid" — these are typed fields, validated here.
        payload = {}
        if params.deliver:
            payload["deliver"] = params.deliver
        if params.deliver_target:
            payload["deliver_target"] = params.deliver_target
        if not params.wake_agent:
            payload["wake_agent"] = False
        if params.rig:
            # 057 WS-A: validated here rather than on the model so an unknown
            # name is REFUSED with the valid list, not stored and silently
            # ignored at dispatch three hours later.
            from core.config_policy.rigs import is_rig, rig_names
            if not is_rig(params.rig):
                return ActionResult(
                    error=f"Unknown tool rig '{params.rig}'. Valid rigs: "
                          f"{', '.join(rig_names())}.",
                    include_in_memory=True)
            # H05: the agent may not grant itself a rig past the self-goal
            # ceiling (money_rail -> defi_trade, ops -> cronjob/goal, ...).
            from tools.goal_tools import agent_rig_refusal, read_taint_authorship_note
            refusal = None if owner_turn else agent_rig_refusal(params.rig)
            if refusal:
                refusal += read_taint_authorship_note(execution_context)
                return ActionResult(error=refusal, include_in_memory=True)
            payload["rig"] = params.rig.strip().lower()
        # 060 WS-5: the job pins its doctrine (seeded every run; no tool granted).
        from core.config_policy.rigs import pinned_skills
        if pinned_skills({"skills": params.skills}):
            payload["skills"] = pinned_skills({"skills": params.skills})
        # H05: provenance — resolve_cron_tools intersects an agent-authored rig;
        # an owner-authored one (owner_authored_turn) is honoured as written.
        # 068 G2: the buy target is DATA on the job, validated before it is stored.
        # 068 B4: a job created by a target-bound run inherits the target (it
        # may only restate the same one).
        from core.wallet.buy_target import PAYLOAD_KEY, inherit_target
        try:
            _target = inherit_target(execution_context, params.target_token)
        except ValueError as exc:
            return ActionResult(error=f"invalid target_token: {exc}", include_in_memory=True)
        if _target:
            payload[PAYLOAD_KEY] = _target
            # DEFI-5: an address the owner did not type is restrict-only.
            from core.wallet.buy_target import TARGET_AUTHOR_KEY, target_provenance
            _tp = target_provenance(execution_context, _target, owner_turn)
            if _tp:
                payload[TARGET_AUTHOR_KEY] = _tp
        # W9: validated (and normalized) by CronService.schedule.
        if params.read_verb:
            payload["read_verb"] = params.read_verb
        if params.pause_windows:
            payload["pause_windows"] = params.pause_windows
        # Impl handoff E: a scheduled WRITE (it spends gas unattended) is the owner's act.
        if params.write_verb:
            if not owner_turn:
                from tools.goal_tools import read_taint_authorship_note
                return ActionResult(error=_WRITE_VERB_OWNER_ONLY
                                    + read_taint_authorship_note(execution_context),
                                    include_in_memory=True)
            payload["write_verb"] = params.write_verb
        from core.config_policy.rigs import AGENT_AUTHOR, AUTHORED_BY_KEY, OWNER_AUTHOR
        payload[AUTHORED_BY_KEY] = OWNER_AUTHOR if owner_turn else AGENT_AUTHOR
        try:
            job = self._resolve_service().schedule(
                task=params.task, schedule_spec=params.schedule,
                user_id=self._user(execution_context),
                payload=payload or None,
                max_duration_seconds=params.max_duration_seconds,
                max_agent_jobs=AGENT_MAX_ACTIVE_JOBS,
            )
        except ScheduleError as e:
            return ActionResult(error=f"Invalid schedule: {e}", include_in_memory=True)
        when = job.next_run_at.isoformat() if job.next_run_at else "?"
        kind = "one-shot" if job.one_shot else "recurring"
        from tools.goal_tools import read_taint_authorship_note
        auth_note = "" if owner_turn else read_taint_authorship_note(execution_context)
        return ActionResult(
            extracted_content=f"Scheduled {kind} cron job `{job.id}` — next run {when}.{auth_note}",
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

    @BaseTool.action("Show one cron job in full: its complete task text, schedule, cap, "
                     "rig, delivery, pinned skills and who authored it. Read this before "
                     "cronjob_edit.", param_model=CronShowAction)
    async def cronjob_show(self, params: CronShowAction, execution_context=None) -> ActionResult:
        job = self._resolve_service().store.get(params.job_id, user_id=self._user(execution_context))
        if job is None:
            return ActionResult(error=f"No such cron job `{params.job_id}`.", include_in_memory=True)
        p = job.payload or {}
        lines = [
            f"Cron job `{job.id}` [{job.status}] — {'one-shot' if job.one_shot else 'recurring'}",
            f"schedule: {job.schedule_spec}",
            f"next run: {job.next_run_at.isoformat() if job.next_run_at else '-'}",
            f"last run: {job.last_run_at.isoformat() if job.last_run_at else '-'}",
            f"max duration: {job.max_duration_seconds}s",
            f"rig: {p.get('rig') or '(default)'}",
            _provider_line(p),
            f"deliver: {p.get('deliver') or '(silent)'}",
            f"skills: {', '.join(p.get('skills') or []) or '(keyword match)'}",
            f"authored by: {_authored_by(job) or 'unstamped (not the owner)'}",
        ]
        if job.status == "cancelled":
            lines.append(_cancel_line(job.id))
        lines += [
            "task:",
            job.task,
        ]
        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)

    @BaseTool.action("Amend a live cron job IN PLACE — patch its task (old_text -> new_text, "
                     "the rest kept verbatim), or change its schedule, cap, rig, delivery or "
                     "pinned skills. Use this instead of cancel + re-schedule, which re-types "
                     "the task and loses its rules.", param_model=CronEditAction)
    async def cronjob_edit(self, params: CronEditAction, execution_context=None) -> ActionResult:
        # Same seat rule as cronjob_schedule: an edit changes what a job that
        # outlives this run will do.
        refusal = self._self_scheduling_refusal(execution_context)
        if refusal:
            return ActionResult(error=refusal.replace("cronjob_schedule", "cronjob_edit"),
                                include_in_memory=True)
        from tools.goal_tools import owner_authored_turn
        owner_turn = owner_authored_turn(execution_context)
        svc = self._resolve_service()
        user = self._user(execution_context)
        job = svc.store.get(params.job_id, user_id=user)
        if job is None:
            return ActionResult(error=f"No such cron job `{params.job_id}`.", include_in_memory=True)
        # Provenance: an autonomous run amends only the jobs it scheduled itself.
        # A job the owner scheduled (or a legacy row with no stamp) — e.g. a money
        # rail and its safety rules — changes only on a genuine owner turn.
        if not owner_turn and _authored_by(job) != "agent":
            return ActionResult(
                error=(f"cronjob_edit denied: `{job.id}` was scheduled by the owner; only an "
                       "owner turn may amend it. Raise it with owner_ask instead."),
                include_in_memory=True)
        updates: dict = {}
        if params.rig is not None:
            from core.config_policy.rigs import is_rig, rig_names
            rig = params.rig.strip().lower()
            if rig in ("none", "", "-"):
                updates["rig"] = None
            elif not is_rig(rig):
                return ActionResult(
                    error=f"Unknown tool rig '{params.rig}'. Valid rigs: {', '.join(rig_names())}.",
                    include_in_memory=True)
            else:
                from tools.goal_tools import agent_rig_refusal
                refusal = None if owner_turn else agent_rig_refusal(rig)
                if refusal:
                    return ActionResult(error=refusal, include_in_memory=True)
                updates["rig"] = rig
        if params.deliver is not None:
            # Validated against the delivery allowlist by CronService.edit.
            target = params.deliver.strip().lower()
            updates["deliver"] = None if target in ("none", "", "-") else target
        if params.skills is not None:
            from core.config_policy.rigs import pinned_skills
            updates["skills"] = pinned_skills({"skills": params.skills}) or None
        # 068 G2. An owner job reaches here only on an owner turn (above).
        # 068 B4: a target-bound run stamps its own target on any job it edits.
        from core.wallet.buy_target import PAYLOAD_KEY, inherit_target, target_from_context
        if params.target_token is not None or target_from_context(execution_context):
            try:
                updates[PAYLOAD_KEY] = inherit_target(execution_context, params.target_token)
            except ValueError as exc:
                return ActionResult(error=f"invalid target_token: {exc}", include_in_memory=True)
            # DEFI-5: a new target is trust only when the owner typed it.
            from core.wallet.buy_target import TARGET_AUTHOR_KEY, target_provenance
            # (None drops the stamp: no target, or one the owner typed.)
            updates[TARGET_AUTHOR_KEY] = target_provenance(
                execution_context, updates[PAYLOAD_KEY], True)
        # W9: validated (and normalized) by CronService.edit; empty clears.
        if params.read_verb is not None:
            updates["read_verb"] = params.read_verb or None
        if params.pause_windows is not None:
            updates["pause_windows"] = params.pause_windows or None
        if params.write_verb is not None:
            if not owner_turn:
                return ActionResult(error=_WRITE_VERB_OWNER_ONLY, include_in_memory=True)
            updates["write_verb"] = params.write_verb or None
        # One rule: an edit never upgrades a job. A non-owner-authored turn only
        # reaches an agent job (refused above otherwise), which stays the agent's;
        # an owner-authored turn keeps the job's authorship as it is.
        try:
            changed = svc.edit(
                job.id, user_id=user, via="agent",
                old_text=params.old_text, new_text=params.new_text,
                schedule_spec=params.schedule,
                max_duration_seconds=params.max_duration_seconds,
                payload_updates=updates or None)
        except ScheduleError as e:
            return ActionResult(error=f"Edit refused: {e}", include_in_memory=True)
        if not changed:
            return ActionResult(error="Nothing to change: pass old_text/new_text, schedule, "
                                      "max_duration_seconds, rig, deliver, skills, target_token, "
                                      "read_verb, write_verb or pause_windows.",
                                include_in_memory=True)
        return ActionResult(
            extracted_content=f"Edited cron job `{job.id}`: {', '.join(changed)}.",
            include_in_memory=True)

    @BaseTool.action("Cancel a scheduled cron job by id.", param_model=CronCancelAction)
    async def cronjob_cancel(self, params: CronCancelAction, execution_context=None) -> ActionResult:
        # DATA-7 without the collateral: a leaf never cancels; a run may cancel
        # the jobs the agent scheduled (that only stops work); a job the owner
        # set is cancelled only by an owner-authored turn.
        from tools.goal_tools import _is_leaf_context, owner_authored_turn
        if _is_leaf_context(execution_context):
            return ActionResult(error="Refused: a leaf/sub-agent cannot cancel a cron job.",
                                include_in_memory=True)
        svc = self._resolve_service()
        job = svc.store.get(params.job_id, user_id=self._user(execution_context))
        if job is not None and _authored_by(job) != "agent" and \
                not owner_authored_turn(execution_context):
            return ActionResult(
                error=(f"cronjob_cancel denied: `{job.id}` was scheduled by the owner; only "
                       "the owner can cancel it (a new owner message, or /cron cancel)."),
                include_in_memory=True)
        ok = svc.cancel(params.job_id, user_id=self._user(execution_context),
                                            via="agent")
        msg = f"Cancelled cron job `{params.job_id}`." if ok else f"No such cron job `{params.job_id}`."
        return ActionResult(extracted_content=msg, include_in_memory=True)


def _cancel_line(job_id: str) -> str:
    """When and through which surface a job was cancelled, from the cron audit
    event (2026-10-06: the owner asked who cancelled a job and Rob could only
    guess). Fail-open: an unreadable log says so rather than inventing a cause."""
    try:
        from core.event_log import open_event_log
        log = open_event_log()
        rows = log.query(kind="cron_cancelled", limit=2000) if log else []
    except Exception:
        rows = None
    if rows is None:
        return "cancelled: (audit log unreadable)"
    for r in rows:
        if (r.get("attrs") or {}).get("job_id") == job_id:
            import datetime as _dt
            when = _dt.datetime.utcfromtimestamp(float(r["ts"])).strftime("%Y-%m-%d %H:%MZ")
            via = (r.get("attrs") or {}).get("via") or "an unrecorded surface (CLI/console before 2026-10-06)"
            return f"cancelled: {when} via {via}"
    return "cancelled: (no audit record)"


def cron_enabled() -> bool:
    """Whether the cron subsystem is turned on (opt-in; off in production by default).

    W1-1: the default is governed by AUTONOMY_POSTURE — only the `full` posture turns
    cron (time-based initiative) on by default; `silent`/`owner-visible` keep it OFF.
    An explicit CRON_ENABLED always wins.
    """
    try:
        from core.config_policy import _posture_autonomy_default
        default = _posture_autonomy_default("CRON_ENABLED")
    except Exception:
        default = False
    return _bool_env("CRON_ENABLED", default)


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
            description="Schedule durable recurring/one-shot agent runs (cronjob_schedule/list/show/edit/cancel)",
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=80,
            is_optional=True,
        ),
        cron_enabled,
        force=force,
    )
