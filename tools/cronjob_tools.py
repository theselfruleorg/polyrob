"""`cronjob` agent tool (roadmap P5 / Reference §30).

Lets an agent schedule durable, recurring or one-shot work that outlives the
current turn — the home for tasks `delegate_task` explicitly cannot do. Thin glue
over :class:`cron.service.CronService`; all scheduling logic lives there.

Off by default: the tool is only registered when ``CRON_ENABLED=true`` and is not
in the default ``tool_ids`` list, so production (``UVICORN_WORKERS=1``) is
unaffected until cron is explicitly turned on.
"""
from __future__ import annotations

from core.runtime_paths import cron_db_path
import os
from typing import Any, Dict, List, Optional


from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from agents.task.loop_types.controller_types import ActionResult
from cron.jobs import CronJobStore
from core.cron_lanes import LaneStopped
from cron.schedule import ScheduleError
from cron.service import CronService


# --- the job toolset ---------------------------------------------------------
# 2026-10-14 review C-9: ONE resolver for the runner (``cron/runner.py``
# re-exports both names) and this tool's results, so the agent cannot tell the
# owner a job "posts by itself" on a rig with no posting tool. It lives here,
# not in its own module, so the cron cycle gains no member (layering ratchet).


def default_cron_tools() -> list:
    """Posture-aware default cron toolset (WS-8). Posture 0: ['filesystem','task']
    (byte-identical). Posture>=1: + the compute tools so a scheduled build/self-env
    run isn't tool-starved. Mirrors ``goals.dispatcher.default_goal_tools``; resolved
    at call time so posture 0 is unchanged."""
    try:
        from agents.task.goals.dispatcher import default_goal_tools
        return default_goal_tools()
    except Exception:
        from agents.task.constants import BASE_DEFAULT_TOOLS
        return list(BASE_DEFAULT_TOOLS)


def resolve_cron_tools(payload: Optional[dict]) -> list:
    """057 WS-A: the job's toolset — ``payload.tools`` (verbatim) >
    ``payload.rig`` > ``AUTONOMOUS_RIG_DEFAULT`` > :func:`default_cron_tools`.
    Byte-identical while the env is unset, which is the shipped default
    (``full``)."""
    from core.config_policy.rigs import resolve_rig_tools, rig_tools
    tools = resolve_rig_tools(payload, default_cron_tools())
    data = dict(payload or {})
    # 085 W8 A4: the job's DECLARED set, re-judged against its stored actor.
    from core.tool_grants import dispatch_recheck
    from tools.goal_tools import _SELF_GOAL_BASELINE_TOOLS, allowed_self_goal_tools
    kept, refused = dispatch_recheck(
        tools, data, declared=data.get("tools") or rig_tools(data.get("rig")) or [],
        agent_ceiling=set(allowed_self_goal_tools()) | set(_SELF_GOAL_BASELINE_TOOLS))
    if refused:
        import logging
        logging.getLogger(__name__).warning(
            "cron job: dropped %s — its writer (%s) cannot grant them", refused,
            data.get("actor"))
    return kept


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
                                                  "an hourly swap job at step 6 before its swap (2026-09-18).")
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
    precheck: Optional[Dict[str, Any]] = Field(
        default=None,
        description="For a WATCH job (monitor/scout/exit/watcher): a $0 check that runs BEFORE "
                    "the model each tick — {'verb': <a read_verb verb>, 'params': {...}, "
                    "'alert': {'field', 'below'?, 'above'?}, 'move_pct': 5, 'field': "
                    "'price_usd', 'max_quiet_hours': 24}. When the read is clean, no alert "
                    "fires and the field moved less than move_pct since the last model run, "
                    "the tick is skipped at $0. Otherwise the run starts with the read "
                    "attached. Use it on every price/pool watch.")
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
                    "UTC. A tick inside a window is a $0 skip (e.g. hold a recurring swap "
                    "from T-1h to 24h after a mint window).")


class CronListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CronCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(..., description="The id of the cron job to cancel.")
    forever: bool = Field(
        False, description="Only when the OWNER asked in this turn: also stop the job's "
                           "lane (its pinned skill, else its rig), so no new job in it is "
                           "created until the owner re-arms it (/job allow <lane>).")


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
                                    description="New schedule spec; the next run is recomputed from now. "
                                                "This is how you run a job SOONER: set a one-shot ISO "
                                                "time a minute ahead. A one-shot spec makes a recurring "
                                                "job one-shot — set its recurring spec back after that run.")
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
    precheck: Optional[Dict[str, Any]] = Field(
        default=None, description="Replace the job's $0 pre-check (see cronjob_schedule); "
                                  "{} clears it.")
    write_verb: Optional[Dict[str, Any]] = Field(
        default=None, description="OWNER TURN ONLY: replace the job's write verb (see "
                                  "cronjob_schedule); {} clears it.")
    pause_windows: Optional[List[List[Any]]] = Field(
        default=None, description="Replace the job's pause windows ([[start, end], ...]); [] clears them.")


_MANDATE_JOB_OWNER_ONLY = (
    "cronjob_edit refused: this job belongs to a trading mandate — the mandate built it and "
    "only the owner changes it (/trade, or /job edit on an owner seat). Follow the job as "
    "written; tell the owner what you would change.")


_WRITE_VERB_OWNER_ONLY = (
    "write_verb refused: a scheduled write job spends from the treasury with no model turn, "
    "so only a genuine owner turn may create or change one. Ask the owner.")


def _precheck_line(payload: dict) -> str:
    pc = payload.get("precheck")
    if not pc:
        return "precheck: none (every run calls the model)"
    bits = [str(pc.get("verb") or "?")]
    if pc.get("alert"):
        bits.append(f"alert {pc['alert']}")
    if pc.get("move_pct"):
        bits.append(f"runs on a {pc['move_pct']}% move of {pc.get('field')}")
    if pc.get("max_quiet_hours"):
        bits.append(f"runs anyway after {pc['max_quiet_hours']} h quiet")
    return "precheck ($0, quiet ticks skip the model): " + "; ".join(bits)


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


def _same_minute_line(svc, job, user_id) -> str:
    """Name every other live job whose next run is this job's next run (2026-10-17:
    a judge "24h after the announce" had the announce's own spec), or ''."""
    if job.next_run_at is None:
        return ""
    try:
        twins = [j for j in svc.list_jobs(user_id=user_id)
                 if j.id != job.id and j.status == "scheduled" and j.next_run_at == job.next_run_at]
    except Exception:
        return ""
    if not twins:
        return ""
    names = "; ".join(f"`{j.id}` ({j.task[:40].strip()})" for j in twins)
    return (f"⚠ next run {job.next_run_at.isoformat()} is the SAME minute as {names} — "
            "they fire together, not one after the other.")


def _tools_line(payload: Optional[dict]) -> str:
    """The tools this job's runs actually carry — the runner's own resolver
    (2026-10-14 review C-9: the agent told the owner a job "posts by itself"
    on a rig that had no posting tool). Fail-open to a named unknown."""
    try:
        ids = resolve_cron_tools(dict(payload or {})) or []
    except Exception as exc:
        return f"its runs carry: UNKNOWN (could not resolve the toolset: {type(exc).__name__})"
    if not ids:
        return "its runs carry: no tools"
    return (f"its runs carry: {', '.join(ids)} — a run can only do what these tools do; "
            f"never tell the owner it will post, trade or send with a tool not listed here")


class CronJobTool(BaseTool):
    """Agent tool exposing schedule/list/show/edit/cancel over the cron job store."""

    def _resolve_service(self) -> CronService:
        if getattr(self, "_cron_service", None) is None:
            from core.runtime_paths import data_dir_or_home
            data_dir = data_dir_or_home(
                getattr(self.config, "data_dir", None) if getattr(self, "config", None) else None)
            self._cron_service = CronService(CronJobStore(cron_db_path(data_dir)))
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
        from tools.goal_tools import owner_seat_turn
        owner_turn = owner_seat_turn(execution_context)
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
            # A turn that is not an owner seat may not request a rig past the
            # self-goal ceiling (money_rail -> defi_trade, ops -> cronjob/goal, ...).
            from tools.goal_tools import agent_rig_refusal
            refusal = None if owner_turn else agent_rig_refusal(params.rig)
            if refusal:
                return ActionResult(error=refusal, include_in_memory=True)
            payload["rig"] = params.rig.strip().lower()
        # 060 WS-5: the job pins its doctrine (seeded every run; no tool granted).
        from core.config_policy.rigs import pinned_skills
        if pinned_skills({"skills": params.skills}):
            payload["skills"] = pinned_skills({"skills": params.skills})
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
            # DEFI-5: owner-typed = trust; an address the owner did not type is restrict-only.
            from core.wallet.buy_target import TARGET_AUTHOR_KEY, target_provenance
            _tp = target_provenance(execution_context, _target, owner_turn)
            if _tp:
                payload[TARGET_AUTHOR_KEY] = _tp
        # W9: validated (and normalized) by CronService.schedule.
        if params.read_verb:
            payload["read_verb"] = params.read_verb
        if params.pause_windows:
            payload["pause_windows"] = params.pause_windows
        if params.precheck:
            payload["precheck"] = params.precheck   # validated by CronService.schedule
        # Impl handoff E: a scheduled WRITE (it spends gas unattended) is the owner's act.
        if params.write_verb:
            if not owner_turn:
                return ActionResult(error=_WRITE_VERB_OWNER_ONLY, include_in_memory=True)
            payload["write_verb"] = params.write_verb
        # 085 W3 (D3): an owner turn's standing MONEY job waits for the owner's tap.
        from tools.goal_tools import money_job_reasons
        _money = money_job_reasons(rig=payload.get("rig"), target=_target) if owner_turn else []
        if _money:
            return await self._money_job_card(params, payload, _target, _money,
                                              execution_context)
        # 085 W8 A4: who wrote the job — the dispatch re-check reads it. A model
        # call stamps the owner seat only for work that is not money (above).
        payload["actor"] = "owner_seat" if owner_turn else "agent"
        try:
            job = self._resolve_service().schedule(
                task=params.task, schedule_spec=params.schedule,
                user_id=self._user(execution_context),
                payload=payload or None,
                max_duration_seconds=params.max_duration_seconds,
                # 0011: the model's call is never the owner's re-arm, even on an
                # owner turn (it may come from recall) — only a typed seat lifts a stop.
                owner_seat=False,
            )
        except LaneStopped as e:
            return ActionResult(error=f"cronjob_schedule refused: {e}", include_in_memory=True)
        except ScheduleError as e:
            return ActionResult(error=f"Invalid schedule: {e}", include_in_memory=True)
        when = job.next_run_at.isoformat() if job.next_run_at else "?"
        kind = "one-shot" if job.one_shot else "recurring"
        from core.cron_lanes import schedule_notes
        notes = "".join(f" {n}" for n in schedule_notes(job))
        return ActionResult(
            extracted_content=(f"Scheduled {kind} cron job `{job.id}` — next run {when}; "
                               f"{_tools_line(job.payload)}.{notes}"),
            include_in_memory=True,
        )

    async def _money_job_card(self, params, payload: dict, target, reasons,
                              execution_context) -> ActionResult:
        """085 W3: the ``/job new <when> | <what> …`` line for this job, as a card."""
        from core.surfaces.cards import card_words
        from tools.goal_tools import grant_option_words, prose_words, raise_job_card
        from core.cron_lanes import check_pinned_skills
        try:   # a card the seat would refuse is not made (an unknown pinned skill)
            check_pinned_skills(payload, user_id=self._user(execution_context))
        except Exception as exc:
            return ActionResult(error=f"cronjob_schedule refused: {exc}", include_in_memory=True)
        grant = grant_option_words(rig=payload.get("rig"), target=target,
                                   skills=payload.get("skills"),
                                   deliver=payload.get("deliver"))
        try:
            words = (["/job", "new"] + card_words(params.schedule) + ["|"]
                     + prose_words(params.task) + grant)
        except ValueError as exc:
            return ActionResult(error=f"Nothing was written: {exc}.", include_in_memory=True)
        skipped = [name for name, value in (
            ("max_duration_seconds", params.max_duration_seconds != 600),
            ("deliver_target", params.deliver_target), ("wake_agent", not params.wake_agent),
            ("read_verb", params.read_verb), ("precheck", params.precheck),
            ("write_verb", params.write_verb), ("pause_windows", params.pause_windows))
            if value]
        return await raise_job_card(
            self, execution_context, words, what="scheduled job", reasons=reasons,
            details=[f"When: {params.schedule}"] + [f"Grant: {w}" for w in grant],
            not_carried=skipped,
            later="find the job with cronjob_list and set those fields with cronjob_edit.")

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
                     "rig, delivery and pinned skills. Read this before "
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
            _tools_line(p),
            _precheck_line(p),
            _provider_line(p),
            f"deliver: {p.get('deliver') or '(silent)'}",
            f"skills: {', '.join(p.get('skills') or []) or '(keyword match)'}",
        ]
        if job.status == "cancelled":
            lines.append(_cancel_line(job.id))
        else:
            lines.append(_same_minute_line(self._resolve_service(), job,
                                           self._user(execution_context)))
        lines = [ln for ln in lines if ln]
        lines += [
            "task:",
            job.task,
        ]
        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)

    @BaseTool.action("Amend a live cron job IN PLACE — patch its task (old_text -> new_text, "
                     "the rest kept verbatim), or change its schedule, cap, rig, delivery or "
                     "pinned skills. Use this instead of cancel + re-schedule, which re-types "
                     "the task and loses its rules. To run a job sooner, set a new schedule "
                     "(e.g. a one-shot ISO time a minute ahead).", param_model=CronEditAction)
    async def cronjob_edit(self, params: CronEditAction, execution_context=None) -> ActionResult:
        # Same seat rule as cronjob_schedule: an edit changes what a job that
        # outlives this run will do.
        refusal = self._self_scheduling_refusal(execution_context)
        if refusal:
            return ActionResult(error=refusal.replace("cronjob_schedule", "cronjob_edit"),
                                include_in_memory=True)
        from tools.goal_tools import owner_seat_turn
        owner_turn = owner_seat_turn(execution_context)
        svc = self._resolve_service()
        user = self._user(execution_context)
        job = svc.store.get(params.job_id, user_id=user)
        if job is None:
            return ActionResult(error=f"No such cron job `{params.job_id}`.", include_in_memory=True)
        # 086 W3: a trading mandate's job is code-built from the owner's card; an
        # agent edit could drop its exit barriers or step outside the mandate gate.
        if (job.payload or {}).get("mandate") and not owner_turn:
            return ActionResult(error=_MANDATE_JOB_OWNER_ONLY, include_in_memory=True)
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
        # 068 B4: a target-bound run stamps its own target on any job it edits.
        from core.wallet.buy_target import PAYLOAD_KEY, inherit_target, target_from_context
        if params.target_token is not None or target_from_context(execution_context):
            try:
                updates[PAYLOAD_KEY] = inherit_target(execution_context, params.target_token)
            except ValueError as exc:
                return ActionResult(error=f"invalid target_token: {exc}", include_in_memory=True)
            # DEFI-5: a new target is trust only when the owner typed it.
            from core.wallet.buy_target import TARGET_AUTHOR_KEY, target_provenance
            # (owner when the owner typed it, agent otherwise; None with no target.)
            updates[TARGET_AUTHOR_KEY] = target_provenance(
                execution_context, updates[PAYLOAD_KEY], owner_turn)
        # W9: validated (and normalized) by CronService.edit; empty clears.
        if params.read_verb is not None:
            updates["read_verb"] = params.read_verb or None
        if params.pause_windows is not None:
            updates["pause_windows"] = params.pause_windows or None
        if params.precheck is not None:
            updates["precheck"] = params.precheck or None
        if params.write_verb is not None:
            if not owner_turn:
                return ActionResult(error=_WRITE_VERB_OWNER_ONLY, include_in_memory=True)
            updates["write_verb"] = params.write_verb or None
        # 085 W3 (D3): a NEW money grant (a money rig, a buy target) on an owner turn
        # waits for the owner's tap; clearing one, or any other field, does not.
        new_target = updates.get(PAYLOAD_KEY) if params.target_token else None
        from tools.goal_tools import money_job_reasons
        _money = (money_job_reasons(rig=updates.get("rig"), target=new_target)
                  if owner_turn else [])
        if _money:
            return await self._money_edit_card(job, updates, new_target, _money,
                                               execution_context)
        # 085 W8 A4: a rig's writer is who set it — the dispatch re-check judges a
        # rig against the stored actor. An agent rig never demotes a job whose
        # declared set is its explicit ``tools`` (they win over the rig at dispatch).
        if updates.get("rig") and owner_turn:
            updates["actor"] = "owner_seat"
        elif "rig" in updates and not owner_turn and not (job.payload or {}).get("tools"):
            updates["actor"] = "agent"
        # 0011: an edit may not pin a lane the owner stopped (on any turn).
        if "skills" in updates or "rig" in updates:
            from core.cron_lanes import check_pinned_skills, gated_lanes, lane_gate
            had = set(gated_lanes(job.payload))
            added = [x for x in gated_lanes({**(job.payload or {}), **updates})
                     if x not in had]
            try:
                lane_gate(svc.store, user_id=user, lanes=added, owner_seat=False)
                check_pinned_skills({"skills": updates.get("skills")}, user_id=user)
            except LaneStopped as e:
                return ActionResult(error=f"cronjob_edit refused: {e}",
                                    include_in_memory=True)
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
            return ActionResult(error="Nothing to change: every value you passed is already "
                                      "the job's current value (or none was passed). To add or "
                                      "change task text, pass BOTH old_text (the exact passage "
                                      "from cronjob_show) and new_text (to ADD text, make new_text the same "
                                      "passage plus your addition); other fields: schedule, "
                                      "max_duration_seconds, rig, deliver, skills, target_token, "
                                      "read_verb, precheck, write_verb or pause_windows.",
                                include_in_memory=True)
        edited = svc.store.get(job.id, user_id=user) or job
        when = edited.next_run_at.isoformat() if edited.next_run_at else "-"
        twin = _same_minute_line(svc, edited, user)
        return ActionResult(
            extracted_content=(f"Edited cron job `{job.id}`: {', '.join(changed)}; "
                               f"next run {when}; {_tools_line(edited.payload)}."
                               + (f" {twin}" if twin else "")),
            include_in_memory=True)

    async def _money_edit_card(self, job, updates: dict, target, reasons,
                               execution_context) -> ActionResult:
        """085 W3: the ``/job edit <id> grant …`` line for a money edit, as a card.
        The edit's other fields are not written either: the card names them."""
        from core.wallet.buy_target import PAYLOAD_KEY, TARGET_AUTHOR_KEY
        from tools.goal_tools import grant_option_words, raise_job_card
        grant = grant_option_words(rig=updates.get("rig"), target=target)
        others = sorted(k for k in updates if k not in ("rig", PAYLOAD_KEY, TARGET_AUTHOR_KEY))
        return await raise_job_card(
            self, execution_context, ["/job", "edit", job.id, "grant"] + grant,
            what="scheduled job grant", reasons=reasons,
            details=[f"Job `{job.id}`: {(job.task or '')[:80]}"]
                    + [f"Grant: {w}" for w in grant],
            not_carried=others,
            later="call cronjob_edit again with only those fields (no rig or target).")

    @BaseTool.action("Cancel a scheduled cron job by id.", param_model=CronCancelAction)
    async def cronjob_cancel(self, params: CronCancelAction, execution_context=None) -> ActionResult:
        # A leaf never cancels.
        from tools.goal_tools import _is_leaf_context
        if _is_leaf_context(execution_context):
            return ActionResult(error="Refused: a leaf/sub-agent cannot cancel a cron job.",
                                include_in_memory=True)
        # 0011: a cancel on a genuine owner turn is the OWNER's stop (via
        # owner_turn); only such a turn may stop the job's lane for good.
        from core.cron_lanes import OWNER_TURN_VIA, stop_job
        from tools.goal_tools import owner_seat_turn
        owner_turn = owner_seat_turn(execution_context)
        if params.forever and not owner_turn:
            return ActionResult(
                error="cronjob_cancel forever=true refused: only the owner stops a lane "
                      "for good, and this is not an owner turn. Cancel without forever, "
                      "or ask the owner (/job stop <id> --forever).",
                include_in_memory=True)
        svc = self._resolve_service()
        user = self._user(execution_context)
        via = OWNER_TURN_VIA if owner_turn else "agent"
        if params.forever:
            job = svc.store.get(params.job_id, user_id=user)
            if job is None:
                return ActionResult(extracted_content=f"No such cron job `{params.job_id}`.",
                                    include_in_memory=True)
            return ActionResult(extracted_content=stop_job(
                svc, job, user_id=user, via=via, forever=True), include_in_memory=True)
        ok = svc.cancel(params.job_id, user_id=user, via=via)
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
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.cron_enabled()


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
