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

_USAGE = ("usage: /cron [list] | /cron add <schedule> <task…> | "
          "/cron cancel <id>\n"
          "  schedule: 30m · every monday 09:00 · '0 9 * * *' · 2026-10-01T09:00")


def _tenant(ctx: CommandContext) -> str:
    return (getattr(ctx, "user_id", "") or "").strip() or "local"


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
    if len(rest) < 2:
        return _USAGE
    from cron.schedule import ScheduleError

    spec, task = rest[0], " ".join(rest[1:]).strip()
    if not task:
        return _USAGE
    try:
        job = _service(write=True).schedule(
            task=task, schedule_spec=spec, user_id=_tenant(ctx), via="repl")
    except ScheduleError as exc:
        return f"{candy.GUTTER}I could not read that schedule: {exc}\n{_USAGE}"
    when = job.next_run_at.isoformat() if job.next_run_at else "—"
    lines = [f"scheduled {job.id[:8]} — {job.schedule_spec}, next run {when}"]
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


def h_cron(ctx: CommandContext) -> None:
    """``/cron [list] | add <schedule> <task…> | cancel <id>``."""
    args = list(getattr(ctx, "args", None) or [])
    verb = args[0].lower() if args else "list"
    try:
        if verb in ("list", ""):
            ctx.emit(_list(ctx), title="cron")
        elif verb in ("add", "schedule"):
            ctx.emit(_add(ctx, args[1:]), title="cron")
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
    "    /cron add 30m <task>        every 30 minutes\n"
    "    /cron add 'every monday 09:00' <task>\n"
    "    /cron add '0 9 * * *' <task>    5-field cron\n"
    "    /cron cancel <id>           stop one\n"
    "\n"
    "  A stored job only runs when scheduled runs are on; if they are not, I\n"
    "  say so instead of implying the job is live.",
    "`polyrob cron` in a shell, `/cron` on Telegram, and the console's Work.",
)
