"""The trading mandate's jobs (086 §3.2) — built by code from the mandate, never by
the model.

* **Hunt** — one per mandate, cadence from the preset; a $0 precheck
  (``defi_data.discovery_count``) wakes the model only when the mandate may take
  an entry and a candidate passed the cheap screen.
* **Exit** — one per open mandate position; a $0 precheck
  (``defi_data.position_mark``) wakes the model only near or past a barrier, and
  ``cancel_when: closed|dust`` retires the job by itself.
* **Review** — one per mandate, daily, research rig, no precheck.

Every job carries ``payload.mandate`` (its lane is ``mandate:<id>``,
``core.cron_lanes.job_lane``) and ``payload.mandate_job`` (its role here).

Where the money authority comes from: the hunt and the review are written by the
owner seat that confirmed the mandate (``core.owner_create.create_cron`` —
``payload.actor = "owner_seat"``, ``owner_seat=True``). An exit job is written
later by the lifecycle (``polyrob/app/trading_lifecycle.py``), from a book
event, under THAT confirmation: :func:`arm_exit` stamps
``payload.actor = "owner_seat"`` only for a mandate whose stored ``actor`` is the
owner seat (the card Confirm), so the cron read's re-check
(``core.tool_grants.dispatch_recheck``) keeps the ``money_rail`` tools. It never
builds an owner turn context, and it schedules with ``owner_seat=False``: an
owner's lane stop (``/job stop --forever``) still refuses it.

Composition root: imports ``cron`` and below.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROLE_KEY = "mandate_job"
HUNT, EXIT, REVIEW, CLOSE_ALL = "hunt", "exit", "review", "close_all"

#: The preset's doctrine skill (086 §3.9). The numbers stay in the preset.
PRESET_SKILLS = {"memecoin": "treasury-trading", "dca": "dca"}
EXIT_SKILLS = ["exits", "trade-execution", "post-trade-verify"]
MONEY_RIG = "money_rail"
REVIEW_RIG = "research"
REVIEW_SPEC = "1d"
#: A job in one of these states still holds its place (a paused job is the owner's).
HOLDING = ("scheduled", "running", "paused")


def service(data_dir: Optional[str] = None):
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    return CronService(CronJobStore(cron_db_path(data_dir)))


def _key(chain: Any, account: Any, address: Any) -> Tuple[str, str, str]:
    from core.trading_report import _key as k
    return k(chain, account, address)


# -- payloads -----------------------------------------------------------------------

def hunt_payload(m) -> Dict[str, Any]:
    skills = ["trading-mandate"]
    if PRESET_SKILLS.get(m.preset):
        skills.append(PRESET_SKILLS[m.preset])
    skills += ["pre-trade-check", "trade-execution"]
    return {"mandate": m.id, ROLE_KEY: HUNT, "skills": skills, "rig": MONEY_RIG,
            "precheck": {"verb": "defi_data.discovery_count",
                         "params": {"chains": list(m.chains)},
                         "alert": {"field": "actionable", "above": 0}}}


def review_payload(m) -> Dict[str, Any]:
    return {"mandate": m.id, ROLE_KEY: REVIEW, "skills": ["trading-mandate"],
            "rig": REVIEW_RIG}


def exit_payload(m, chain: str, account: str, address: str) -> Dict[str, Any]:
    pos = {"chain": chain, "account": account or "", "address": address}
    return {"mandate": m.id, ROLE_KEY: EXIT, "position": dict(pos),
            "skills": list(EXIT_SKILLS), "rig": MONEY_RIG,
            "precheck": {"verb": "defi_data.position_mark", "params": dict(pos),
                         "alert": [{"field": "barrier", "not_equals": "none"}],
                         "cancel_when": ["closed", "dust"]}}


#: The hunt task's line the lifecycle rewrites when the stage moves.
STAGE_MARK = "Stage: "
#: The exit task's line the lifecycle replaces once the entry's plan is stored.
PLAN_NONE = "Plan: none recorded yet."


def stage_line(m) -> str:
    """The stage ticket and the preset's tier rungs under it — numbers from the
    mandate and the preset only."""
    from core.trading_mandate import stage_ticket
    p = m.effective_preset()
    cap = stage_ticket(m, p)
    rungs = ", ".join(f"{tier} at most ${min(float(usd), cap):.2f}"
                      for tier, usd in p.tier_ladder)
    return (f"{STAGE_MARK}{m.stage} — one entry costs at most ${cap:.2f} now. Tier rungs: "
            f"{rungs or 'one size, the stage ticket'}.")


def hunt_task(m) -> str:
    return (f"Hunt for trading mandate {m.id} ({m.preset} on {', '.join(m.chains)}). The $0 "
            f"precheck woke you: the mandate may take an entry and discovery has a "
            f"candidate. Screen it (pre-trade-check), size it by the rungs below and enter "
            f"through trade-execution with a plan, or skip. The exit job is armed by code "
            f"when the buy lands — never create, edit or cancel a job yourself.\n"
            f"{stage_line(m)}")


def review_task(m) -> str:
    return (f"Daily review of trading mandate {m.id}: read /trade status figures (the book "
            f"is the only record), what fired, the breaker and stage state, and the LLM "
            f"cost beside the P&L. Answer the four learning questions of the "
            f"trading-mandate skill. Change nothing; report.")


def plan_line(user_id: str, chain: str, account: str, address: str) -> str:
    """``Plan: <the stored plan>`` of the position, or :data:`PLAN_NONE`."""
    try:
        from core.position_plans import plan_for
        plan = plan_for(user_id, chain, address, account=account or "")
    except Exception:
        logger.warning("exit job: the plan of %s on %s is unreadable", address, chain,
                       exc_info=True)
        return PLAN_NONE
    return f"Plan: {plan.render()}" if plan is not None else PLAN_NONE


def exit_task(m, symbol: str, chain: str, address: str, plan: str = PLAN_NONE) -> str:
    return (f"Exit watch for {symbol or address} on {chain} (trading mandate {m.id}, exit "
            f"preset {m.exit_preset}). The $0 precheck woke you: a barrier is near or "
            f"crossed, the time stop is due, or the price is unreadable. Follow the "
            f"position's plan; confirm with the exit quote; sell into the quote asset when "
            f"a barrier fires; stops only tighten. Never create, edit or cancel a job.\n"
            f"{plan}")


def _every(minutes: int) -> str:
    return f"{int(minutes)}m"


# -- reads --------------------------------------------------------------------------

def mandate_jobs(svc, *, user_id: Optional[str] = None,
                 mandate_id: Optional[str] = None) -> List[Any]:
    """Every cron job carrying ``payload.mandate`` (any state). Raises on an
    unreadable cron store."""
    out = []
    for j in svc.store.list(user_id=user_id):
        p = j.payload if isinstance(j.payload, dict) else {}
        mid = str(p.get("mandate") or "")
        if mid and (mandate_id is None or mid == mandate_id):
            out.append(j)
    return out


def role(job) -> str:
    p = job.payload if isinstance(job.payload, dict) else {}
    r = str(p.get(ROLE_KEY) or "")
    if r:
        return r
    if isinstance(p.get("position"), dict):
        return EXIT
    return REVIEW if p.get("rig") == REVIEW_RIG else HUNT


def holding(job) -> bool:
    return bool(job.enabled) and job.status in HOLDING


def position_key(job) -> Optional[Tuple[str, str, str]]:
    pos = (job.payload or {}).get("position")
    if not isinstance(pos, dict):
        return None
    return _key(pos.get("chain"), pos.get("account"), pos.get("address"))


# -- writes -------------------------------------------------------------------------

def create_mandate_jobs(svc, m, *, via: str) -> List[Any]:
    """The owner seat's half (``/trade start``): the hunt and the review, written
    with the owner's grant. Exit jobs are armed per position by the lifecycle."""
    from core.owner_create import create_cron
    p = m.effective_preset()
    made = [create_cron(svc, task=hunt_task(m), schedule_spec=_every(p.hunt_cadence_min),
                        user_id=m.user_id, via=via, extra_payload=hunt_payload(m))]
    made.append(create_cron(svc, task=review_task(m), schedule_spec=REVIEW_SPEC,
                            user_id=m.user_id, via=via, extra_payload=review_payload(m)))
    return made


def arm_exit(svc, m, *, chain: str, account: str, address: str,
             symbol: Optional[str] = None) -> Optional[Any]:
    """Schedule the exit job of one mandate position, unless one already holds
    its place. Returns the new job, or None (already armed / no exit preset).
    Raises ``core.cron_lanes.LaneStopped`` when the owner stopped the lane."""
    from core.tool_grants import ACTOR_KEY, ACTOR_OWNER
    from core.trading_mandate import OWNER_SEAT
    if m.actor != OWNER_SEAT:
        raise ValueError(f"mandate {m.id} was not confirmed by the owner seat; no exit job")
    p = m.effective_preset()
    if p.exit is None or not p.exit_cadence_min:
        return None
    want = _key(chain, account, address)
    for j in mandate_jobs(svc, user_id=m.user_id, mandate_id=m.id):
        if role(j) == EXIT and holding(j) and position_key(j) == want:
            return None
    payload = exit_payload(m, want[0], want[1], address)
    payload[ACTOR_KEY] = ACTOR_OWNER  # the mandate's card Confirm IS this job's grant
    plan = plan_line(m.user_id, want[0], want[1], address)
    return svc.schedule(task=exit_task(m, symbol or "", want[0], address, plan),
                        schedule_spec=_every(p.exit_cadence_min), user_id=m.user_id,
                        payload=payload, via="mandate", owner_seat=False)


def _swap_line(svc, job, user_id: str, prefix: str, new_line: str) -> bool:
    """Rewrite the ONE task line starting with *prefix* (the cron edit's own
    patch rule). True when the job changed."""
    old = next((ln for ln in job.task.splitlines() if ln.startswith(prefix)), None)
    if old is None or old == new_line or job.task.count(old) != 1:
        return False
    return bool(svc.edit(job.id, user_id=user_id, via="mandate", old_text=old,
                         new_text=new_line))


def refresh_stage(svc, m) -> List[str]:
    """After a stage move: the hunt task states the new stage ticket and rungs."""
    done = []
    for j in mandate_jobs(svc, user_id=m.user_id, mandate_id=m.id):
        if role(j) == HUNT and holding(j) and _swap_line(svc, j, m.user_id, STAGE_MARK,
                                                         stage_line(m)):
            done.append(j.id)
    return done


def refresh_plan(svc, job, user_id: str) -> bool:
    """An exit job armed before its entry's plan was stored gets the plan line."""
    if PLAN_NONE not in (job.task or ""):
        return False
    pos = (job.payload or {}).get("position") or {}
    line = plan_line(user_id, str(pos.get("chain") or ""), str(pos.get("account") or ""),
                     str(pos.get("address") or ""))
    return line != PLAN_NONE and _swap_line(svc, job, user_id, PLAN_NONE, line)


def cancel_exit(svc, *, user_id: str, chain: str, account: str, address: str,
                via: str = "mandate", mandate_id: Optional[str] = None) -> List[str]:
    """Cancel every live exit job of one position. Returns the cancelled ids."""
    want = _key(chain, account, address)
    done = []
    for j in mandate_jobs(svc, user_id=user_id, mandate_id=mandate_id):
        if role(j) == EXIT and holding(j) and position_key(j) == want:
            if svc.cancel(j.id, user_id=user_id, via=via):
                done.append(j.id)
    return done


def set_hunt(svc, m, *, paused: bool, via: str) -> List[str]:
    """Pause (or resume) the mandate's hunt job(s). Returns the ids that moved."""
    moved = []
    for j in mandate_jobs(svc, user_id=m.user_id, mandate_id=m.id):
        if role(j) != HUNT or not j.enabled:
            continue
        if paused and j.status in ("scheduled", "running"):
            if svc.pause(j.id, user_id=m.user_id, via=via):
                moved.append(j.id)
        elif not paused and j.status == "paused":
            if svc.resume(j.id, user_id=m.user_id, via=via):
                moved.append(j.id)
    return moved


def cancel_programme(svc, m, *, via: str) -> List[str]:
    """A stopped mandate: cancel its hunt and review; exit jobs keep running."""
    done = []
    for j in mandate_jobs(svc, user_id=m.user_id, mandate_id=m.id):
        if role(j) in (HUNT, REVIEW) and holding(j):
            if svc.cancel(j.id, user_id=m.user_id, via=via):
                done.append(j.id)
    return done


def schedule_close_all(svc, m, *, via: str, now: Optional[datetime] = None):
    """The owner's "close all": ONE run, a minute from now, that sells every open
    mandate position into the quote asset (an exit, so a stopped mandate admits
    it). Written with the owner's grant."""
    from core.owner_create import create_cron
    at = ((now or datetime.now()) + timedelta(minutes=1)).replace(microsecond=0)
    payload = {"mandate": m.id, ROLE_KEY: CLOSE_ALL, "skills": list(EXIT_SKILLS),
               "rig": MONEY_RIG}
    task = (f"Close all: trading mandate {m.id} was stopped and the owner chose to close "
            f"every open position. Sell each open mandate position (see /trade status) "
            f"fully into the quote asset through trade-execution, with the exit quote; a "
            f"position you cannot sell is reported with why. Buy nothing.")
    return create_cron(svc, task=task, schedule_spec=at.isoformat(), user_id=m.user_id,
                       via=via, extra_payload=payload)


# -- the price read (dust, /trade status) -------------------------------------------

def spend_grade_price(chain: str, address: str) -> Optional[float]:
    """The token's USD price when the agreed quote is spend grade (``high``),
    else None. Never raises."""
    try:
        from tools.defi import price_sources
        info = price_sources.quote(chain, address)
    except Exception:
        return None
    if info is None or str(getattr(info, "confidence", "") or "") != "high":
        return None
    try:
        price = float(getattr(info, "price_usd", None))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


__all__ = ["CLOSE_ALL", "EXIT", "HUNT", "REVIEW", "ROLE_KEY", "arm_exit", "cancel_exit",
           "cancel_programme", "create_mandate_jobs", "exit_payload", "holding",
           "hunt_payload", "mandate_jobs", "position_key", "review_payload", "role",
           "schedule_close_all", "service", "set_hunt", "spend_grade_price"]
