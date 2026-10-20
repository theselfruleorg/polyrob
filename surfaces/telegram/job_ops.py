"""`/jobs` and `/job …` — the owner's scheduled jobs on every chat seat (080 WS-2).

One handler for Telegram, the console chat box (both through
``surfaces/telegram/admin_verbs.py``) and the REPL (``cli/ui/commands/h_job.py``
calls :func:`jobs_reply`). It is thin plumbing over ``cron.service.CronService``
— the same service the ``polyrob job`` CLI group, the agent's ``cronjob`` tool
and the ticker use — so no scheduling rule lives here.

Owner words: a "job" (glossary: scheduled job). Its pause is the job's OWN
state: a paused job shows "paused" and no next run. ``stop`` ends it for good
and records which seat stopped it; ``stop --forever`` also stops the job's lane
and ``allow <lane>`` re-arms it (``core.cron_lanes``, work order 0011). The
scheduler-off sentence comes from ONE helper (``cron.service.scheduler_off_note``)
on every seat.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Rows one chat listing renders before collapsing the rest into a count.
_LIST_LIMIT = 10
#: Characters of the job's instruction a list row shows.
_HEAD = 60

USAGE = ("Usage: /jobs · /job new <when> <what> · /job show <id> · "
         "/job edit <id> schedule|what <value> · /job pause|resume|run|stop <id> · "
         "/job stop <id> --forever (stop its lane) · /job allow <lane>\n"
         "<when>: 30m · every monday 09:00 · '0 9 * * *' · 2026-10-20T09:00 — "
         "quote it or end it with |\n"
         "Options on /job new: `tools=a,b` grants those abilities; "
         "`target=<token address> chain=<network>` is the one token it may buy.")
_NEW_EXAMPLE = "e.g. /job new every monday 09:00 | summarise the week"


def _code(text: Any) -> str:
    return f"`{text}`"


def _service(data_dir: str):
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    return CronService(CronJobStore(cron_db_path(data_dir)))


def _with_note(text: str) -> str:
    from cron.service import scheduler_off_note
    note = scheduler_off_note()
    return f"{text}\n⚠️ {note}" if note else text


def _head(job) -> str:
    text = " ".join((job.task or "").split())
    return text if len(text) <= _HEAD else text[:_HEAD - 1] + "…"


def _row(job) -> str:
    from cron.service import last_outcome, next_run_text
    return (f"• {_code(job.id[:8])} {_head(job)}\n"
            f"   {job.schedule_spec} · next {next_run_text(job)} · "
            f"last {last_outcome(job)}")


def _grant_note(payload) -> str:
    """What the owner granted the job, read back from the stored payload."""
    p = payload or {}
    parts = []
    if p.get("tools"):
        parts.append("tools " + ", ".join(p["tools"]))
    t = p.get("target_token")
    if t:
        parts.append(f"target {t['address']} on {t['chain']}")
    return ("\nGranted: " + "; ".join(parts) + ".") if parts else ""


def _resolve(svc, user_id: str, token: str, *, live: bool = True):
    """``(job, error)`` for a short id prefix, tenant-scoped. *live* limits the
    match to jobs that have not ended (a stopped job is not paused or run)."""
    from surfaces.telegram.owner_ops import resolve_prefix
    from cron.service import LIVE_STATUSES
    jobs = [j for j in svc.list_jobs(user_id=user_id)
            if not live or j.status in LIVE_STATUSES]
    job_id, err = resolve_prefix(token, [j.id for j in jobs])
    if err:
        return None, f"{err} — see /jobs."
    return next(j for j in jobs if j.id == job_id), None


def _stopped(svc, user_id: str) -> str:
    """0011: the lanes the owner stopped, as a trailing section ("" when none)."""
    from core.cron_lanes import stopped_lines
    lines = stopped_lines(svc.store, user_id=user_id)
    return ("\n\n" + "\n".join(lines)) if lines else ""


def _list(svc, user_id: str) -> str:
    from cron.service import LIVE_STATUSES
    jobs = [j for j in svc.list_jobs(user_id=user_id) if j.status in LIVE_STATUSES]
    if not jobs:
        return _with_note("No scheduled jobs.\nAdd one: /job new <when> <what>\n"
                          + _NEW_EXAMPLE + _stopped(svc, user_id))
    shown = jobs[:_LIST_LIMIT]
    lines = [f"{len(jobs)} scheduled job(s):"] + [_row(j) for j in shown]
    if len(jobs) > len(shown):
        lines.append(f"(+{len(jobs) - len(shown)} more — see the console or ask me)")
    return _with_note("\n".join(lines) + _stopped(svc, user_id))


def _show(svc, user_id: str, rest: List[str]) -> str:
    if not rest:
        return "Usage: /job show <id>"
    job, err = _resolve(svc, user_id, rest[0], live=False)
    if err:
        return err
    out = [_row(job),
           f"   state {job.status} · created {job.created_at} · "
           f"max duration {job.max_duration_seconds}s"]
    paused = (job.payload or {}).get("paused") or {}
    if paused:
        out.append(f"   paused via {paused.get('via') or '-'} at {paused.get('at') or '-'}")
    stopped = (job.payload or {}).get("stopped") or {}
    if stopped:
        out.append(f"   stopped via {stopped.get('via') or '-'} at {stopped.get('at') or '-'}")
    from core.cron_lanes import lane_line
    lane = lane_line(svc.store, user_id=user_id, payload=job.payload)
    if lane:
        out.append(f"   {lane}")
    # 060 WS-4: the job's prose IS an instruction (the SCOPED tier).
    from core.rules_sweep import rail_instruction_lines
    out += ["   " + line for line in rail_instruction_lines(job.task, job.payload)]
    return "\n".join(out)


def _new(svc, user_id: str, rest: List[str], via: str) -> str:
    from core.owner_create import create_cron, cron_options_payload, split_cron_options
    from cron.schedule import ScheduleError
    from cron.service import parse_new_job_args
    try:
        rest, options = split_cron_options(rest)
        tools, extra = cron_options_payload(options)
    except ValueError as e:
        return f"❌ {e}\n{USAGE}"
    if not rest:
        return f"{USAGE}\n{_NEW_EXAMPLE}"
    try:
        spec, what = parse_new_job_args(rest)
    except ScheduleError as e:
        return f"❌ {e}\n{USAGE}"
    dup = svc.find_duplicate(user_id=user_id, task=what, schedule_spec=spec)
    if dup is not None:
        from cron.service import duplicate_job_text
        return "⚠️ " + duplicate_job_text(dup)
    try:
        job = create_cron(svc, task=what, schedule_spec=spec, user_id=user_id,
                          tools=tools, via=via, extra_payload=extra)
    except ScheduleError as e:
        return f"Invalid schedule {_code(spec)}: {e}"
    except ValueError as e:
        return f"❌ {e}"
    from core.cron_lanes import schedule_notes
    from cron.service import next_run_text
    notes = "".join(f"\n{n}" for n in schedule_notes(job))
    return _with_note(f"✅ Scheduled job {_code(job.id[:8])} ({spec}) — next run "
                      f"{next_run_text(job)}." + _grant_note(job.payload) + notes)


def _edit(svc, user_id: str, rest: List[str], via: str) -> str:
    usage = "Usage: /job edit <id> schedule <when> · /job edit <id> what <new text>"
    if len(rest) < 3:
        return usage
    job, err = _resolve(svc, user_id, rest[0])
    if err:
        return err
    field = {"what": "task"}.get(rest[1].lower(), rest[1])
    from core.owner_create import edit_cron
    from cron.schedule import ScheduleError
    try:
        changed = edit_cron(svc, job, user_id=user_id, field=field,
                            value=" ".join(rest[2:]), via=via)
    except (ScheduleError, ValueError) as e:
        return f"❌ {e}\n{usage}"
    if not changed:
        return f"Nothing changed on {_code(job.id[:8])}."
    words = ", ".join({"task": "what it does"}.get(c, c) for c in changed)
    return f"✅ Edited {_code(job.id[:8])}: {words}. The rest of the job is kept."


def _state_op(svc, user_id: str, sub: str, rest: List[str], via: str) -> str:
    from surfaces.telegram.owner_ops import FOREVER_FLAGS as flag
    forever = sub == "stop" and any(r in flag for r in rest)
    rest = [r for r in rest if r not in flag] if sub == "stop" else rest
    if not rest:
        return f"Usage: /job {sub} <id>" + (" [--forever]" if sub == "stop" else "")
    # A --forever stop may name a job that already ended (its lane is the point).
    job, err = _resolve(svc, user_id, rest[0], live=not forever)
    if err:
        return err
    short = _code(job.id[:8])
    if sub == "pause":
        if svc.pause(job.id, user_id=user_id, via=via):
            return f"⏸ Paused job {short}. It will not run until /job resume {job.id[:8]}."
        return f"Job {short} is {job.status} — nothing to pause."
    if sub == "resume":
        if svc.resume(job.id, user_id=user_id, via=via):
            from cron.service import next_run_text
            fresh = svc.store.get(job.id, user_id=user_id)
            return _with_note(f"▶ Resumed job {short} — next run {next_run_text(fresh)}.")
        return f"Job {short} is {job.status}, not paused — nothing to resume."
    if sub == "run":
        why = svc.run_now(job.id, user_id=user_id, via=via)
        if not why:
            return _with_note(f"▶ Job {short} runs on the next scheduler pass.")
        if why == "paused":
            return f"Job {short} is paused — /job resume {job.id[:8]} first."
        return f"Job {short} is {why} — I cannot run it now."
    # stop — once, or --forever (also the job's lane: core.cron_lanes.stop_job)
    from core.cron_lanes import stop_job
    return stop_job(svc, job, user_id=user_id, via=via, forever=forever,
                    reason=" ".join(rest[1:]))


def jobs_reply(user_id: str, data_dir: str, args: List[str], *,
               via: str = "telegram") -> str:
    """`/job <sub> …` (and `/jobs` = `/job list`). *via* names the seat on
    every write so the job row and the audit say who changed it."""
    svc = _service(data_dir)
    sub = (args[0].lower() if args else "list")
    rest = list(args[1:])
    if sub in ("list", "ls"):
        return _list(svc, user_id)
    if sub == "show":
        return _show(svc, user_id, rest)
    if sub == "new":
        return _new(svc, user_id, rest, via)
    if sub == "edit":
        return _edit(svc, user_id, rest, via)
    if sub in ("pause", "resume", "run", "stop"):
        return _state_op(svc, user_id, sub, rest, via)
    if sub == "allow":
        from core.cron_lanes import allow_lane
        return allow_lane(svc.store, user_id=user_id, lane=" ".join(rest))
    return USAGE


def jobs_list_reply(user_id: str, data_dir: str, args: Optional[List[str]] = None) -> str:
    """`/jobs` — the list (any argument is ignored)."""
    return jobs_reply(user_id, data_dir, ["list"])
