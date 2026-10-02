"""``/cron`` — durable scheduled runs from the REPL: list, add, cancel (C17).

A terminal-native window onto the durable cron store (roadmap P5). Until
2026-09-21 this handler was deliberately read-only, which left the REPL the one
interactive owner seat that could SEE a schedule and not change it — while
``core.verbs`` already described ``/cron`` as "Durable scheduled runs: list,
add, or cancel" on every seat. It now writes, through the SAME
``cron.service.CronService`` the ``polyrob cron`` group, the agent's ``cronjob``
tool and the lifespan ticker use; no scheduling logic lives here.

Home resolution is the ONE owner/admin seam (``cli._admin_home.admin_data_dir``,
C2), so the jobs listed and added here are the jobs the DAEMON runs — not a
second store under ``cwd/.polyrob``.

Honest states (C47): "no jobs" and "the ticker is off" are two different facts
and are never merged into one guess. A missing DB is "no jobs yet"; it is never
CREATED by a read.
"""

from __future__ import annotations

import os
from typing import List, Optional

from cli.ui import candy
from cli.ui.commands.registry import CommandContext

_USAGE = ("usage: /cron [list] | /cron show <id> | /cron add <schedule> <task…> "
          "[tools=a,b] [target=<token address> chain=<chain>] | "
          "/cron edit <id> schedule|task <value> | /cron cancel <id>\n"
          "  schedule: 30m · every monday 09:00 · '0 9 * * *' · 2026-10-01T09:00")


def _tenant(ctx: CommandContext) -> str:
    from cli._admin_home import admin_owner_tenant
    return admin_owner_tenant(getattr(ctx, "user_id", None))


def _db_path() -> str:
    from cli._admin_home import admin_data_dir
    from core.runtime_paths import cron_db_path
    return cron_db_path(admin_data_dir(write=False))


def _ticker_note() -> str:
    """One honest line when a stored job has no ticker to run it.

    ⚠️ Never a disjunction and never a flag name: the owner is told the state
    and the ONE verb that changes it.
    """
    try:
        from tools.cronjob_tools import cron_enabled
        if cron_enabled():
            return ""
    except Exception:
        # The resolver itself refused — say that, rather than claim "off".
        return (f"{candy.GUTTER}⚠ I could not read whether scheduled runs are "
                f"on; a stored job may or may not run.")
    return (f"{candy.GUTTER}⚠ scheduled runs are off — a stored job waits but "
            f"nothing runs it. Turn them on with `polyrob autonomy on`.")


def _service(write: bool):
    from cli._admin_home import admin_data_dir
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    return CronService(CronJobStore(cron_db_path(admin_data_dir(write=write))))


def _job_lines(jobs: List) -> List[str]:
    lines = [f"Cron jobs ({len(jobs)}):"]
    for job in jobs[:20]:
        when = job.next_run_at.isoformat() if job.next_run_at else "—"
        state = job.status if job.enabled else f"{job.status} (disabled)"
        task_preview = (job.task or "").replace("\n", " ")[:48]
        lines.append(candy.status_line(
            job.status,
            f"{job.id[:8]} [{state}] {job.schedule_spec} -> {when}: {task_preview}",
        ))
    if len(jobs) > 20:
        lines.append(f"{candy.GUTTER}… (+{len(jobs) - 20} more)")
    return lines


def _list(ctx: CommandContext) -> str:
    path = _db_path()
    if not os.path.exists(path):
        # A read NEVER creates the store. No file = nothing scheduled yet, and
        # that is a different fact from "the ticker is off" — both are said.
        note = _ticker_note()
        body = candy.empty("cron jobs scheduled",
                           "add one with `/cron add 30m <task>`")
        return f"{body}\n{note}" if note else body
    jobs = _service(write=False).list_jobs(user_id=_tenant(ctx))
    if not jobs:
        note = _ticker_note()
        body = candy.empty("cron jobs scheduled",
                           "add one with `/cron add 30m <task>`")
        return f"{body}\n{note}" if note else body
    lines = _job_lines(jobs)
    note = _ticker_note()
    if note:
        lines.append(note)
    lines.append(f"{candy.GUTTER}cancel one: /cron cancel <id>")
    return "\n".join(lines)


def _add(ctx: CommandContext, rest: List[str]) -> str:
    # O13/A14: the SAME option parser and owner-create helper Telegram uses.
    from core.owner_create import create_cron, cron_options_payload, split_cron_options
    try:
        rest, options = split_cron_options(rest)
        tools, extra = cron_options_payload(options)
    except ValueError as exc:
        return f"{candy.GUTTER}{exc}\n{_USAGE}"
    if len(rest) < 2:
        return _USAGE
    from cron.schedule import ScheduleError

    spec, task = rest[0], " ".join(rest[1:]).strip()
    if not task:
        return _USAGE
    try:
        job = create_cron(_service(write=True), task=task, schedule_spec=spec,
                          user_id=_tenant(ctx), tools=tools, via="repl",
                          extra_payload=extra)
    except ScheduleError as exc:
        return f"{candy.GUTTER}I could not read that schedule: {exc}\n{_USAGE}"
    except ValueError as exc:
        return f"{candy.GUTTER}{exc}"
    when = job.next_run_at.isoformat() if job.next_run_at else "—"
    lines = [f"scheduled {job.id[:8]} — {job.schedule_spec}, next run {when}"]
    payload = job.payload or {}
    if payload.get("tools"):
        lines.append(f"{candy.GUTTER}granted tools: {', '.join(payload['tools'])}")
    if payload.get("target_token"):
        t = payload["target_token"]
        lines.append(f"{candy.GUTTER}target: {t['address']} on {t['chain']}")
    note = _ticker_note()
    if note:
        lines.append(note)
    return "\n".join(lines)


def _cancel(ctx: CommandContext, rest: List[str]) -> str:
    if not rest:
        return "usage: /cron cancel <id>   (see /cron)"
    job_id: Optional[str] = rest[0]
    service = _service(write=True)
    tenant = _tenant(ctx)
    if service.cancel(job_id, user_id=tenant, via="repl"):
        return f"cancelled {job_id}"
    # An id that does not match is not the same as a store that refused; the
    # store above would have raised. So this is honestly "no such job here".
    matches = [j for j in service.list_jobs(user_id=tenant)
               if j.id.startswith(job_id)]
    if len(matches) == 1 and service.cancel(matches[0].id, user_id=tenant,
                                            via="repl"):
        return f"cancelled {matches[0].id}"
    if len(matches) > 1:
        return (f"{candy.GUTTER}'{job_id}' matches {len(matches)} jobs — "
                f"use the full id (see /cron).")
    return f"no scheduled job '{job_id}' for tenant {tenant} — see /cron"


def _edit(ctx: CommandContext, rest: List[str]) -> str:
    """O13: ``/cron edit <id> schedule|task <value>`` — in place, payload kept."""
    if len(rest) < 3:
        return "usage: /cron edit <id> schedule <schedule> | /cron edit <id> task <new task>"
    from core.owner_create import edit_cron
    from cron.schedule import ScheduleError
    service = _service(write=True)
    tenant = _tenant(ctx)
    jobs = [j for j in service.list_jobs(user_id=tenant) if j.id.startswith(rest[0])]
    if len(jobs) != 1:
        return (f"{candy.GUTTER}'{rest[0]}' matches {len(jobs)} jobs — "
                "use a longer id (see /cron).")
    try:
        changed = edit_cron(service, jobs[0], user_id=tenant, field=rest[1],
                            value=" ".join(rest[2:]), via="repl")
    except (ScheduleError, ValueError) as exc:
        return f"{candy.GUTTER}{exc}"
    if not changed:
        return f"nothing changed on {jobs[0].id[:8]}"
    return f"edited {jobs[0].id[:8]}: {', '.join(changed)} (the rest of the job is kept)"


def _show(ctx: CommandContext, rest: List[str]) -> str:
    """``/cron show <id>`` — one job, its prose rendered as the instruction it is
    (060 WS-4: the SCOPED tier — a later owner rule outranks it)."""
    if not rest:
        return "usage: /cron show <id>   (see /cron)"
    if not os.path.exists(_db_path()):
        return candy.empty("cron jobs scheduled", "add one with `/cron add 30m <task>`")
    jobs = [j for j in _service(write=False).list_jobs(user_id=_tenant(ctx))
            if j.id.startswith(rest[0])]
    if len(jobs) != 1:
        return (f"{candy.GUTTER}'{rest[0]}' matches {len(jobs)} jobs — "
                "use a longer id (see /cron).")
    job = jobs[0]
    from core.rules_sweep import rail_instruction_lines
    when = job.next_run_at.isoformat() if job.next_run_at else "—"
    lines = [f"{job.id} [{job.status}] {job.schedule_spec} -> {when}",
             f"last run {job.last_run_at or '—'} · max duration {job.max_duration_seconds}s"]
    lines += rail_instruction_lines(job.task, job.payload)
    return "\n".join(lines)


def h_cron(ctx: CommandContext) -> None:
    """``/cron [list] | add <schedule> <task…> | cancel <id>``."""
    args = list(getattr(ctx, "args", None) or [])
    verb = args[0].lower() if args else "list"
    try:
        if verb in ("list", ""):
            ctx.emit(_list(ctx), title="cron")
        elif verb == "show":
            ctx.emit(_show(ctx, args[1:]), title="cron")
        elif verb in ("add", "schedule"):
            ctx.emit(_add(ctx, args[1:]), title="cron")
        elif verb == "edit":
            ctx.emit(_edit(ctx, args[1:]), title="cron")
        elif verb in ("cancel", "remove", "rm"):
            ctx.emit(_cancel(ctx, args[1:]), title="cron")
        else:
            ctx.emit(f"unknown /cron verb {verb!r}.\n{_USAGE}", title="cron")
    except Exception as e:  # fail-open: a locked/absent store is never a crash
        ctx.emit(f"{candy.GUTTER}(cron unavailable: {e})", title="cron")


HELP_CRON = (
    "  Durable scheduled runs: work that survives a restart, unlike a\n"
    "  delegation. Each job runs as its own session on its own clock.\n"
    "\n"
    "    /cron                       what is scheduled\n"
    "    /cron show <id>             one job and the instruction it runs\n"
    "    /cron add 30m <task>        every 30 minutes\n"
    "    /cron add 'every monday 09:00' <task>\n"
    "    /cron add '0 9 * * *' <task>    5-field cron\n"
    "    /cron add 1d <task> tools=defi_trade,defi_data target=0x… chain=base\n"
    "                                grant tools; name the one token it may buy\n"
    "    /cron edit <id> schedule 2h  change one field; the rest is kept\n"
    "    /cron edit <id> task <text>\n"
    "    /cron cancel <id>           stop one\n"
    "\n"
    "  A stored job only runs when scheduled runs are on; if they are not, I\n"
    "  say so instead of implying the job is live.",
    "`polyrob cron` in a shell, `/cron` on Telegram, and the console's Work.",
)
