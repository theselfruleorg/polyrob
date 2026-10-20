"""``/jobs`` and ``/job …`` — scheduled jobs from the REPL (080 WS-2).

The REPL runs the SAME handler as Telegram and the console chat box
(``surfaces.telegram.job_ops.jobs_reply``), so the grammar, the words and the
scheduler-off sentence are one on every seat. This module only resolves the
REPL's tenant and data home through the ONE owner/admin seam
(``cli._admin_home``, C2), so the jobs listed and added here are the jobs the
DAEMON runs — not a second store under ``cwd/.polyrob``.

A read never creates the store: with no ``cron.db`` the list is "no jobs yet".
"""

from __future__ import annotations

import os

from cli.ui import candy
from cli.ui.commands.registry import CommandContext

_WRITES = ("new", "edit", "pause", "resume", "run", "stop", "allow")


def _tenant(ctx: CommandContext) -> str:
    from cli._admin_home import admin_owner_tenant
    return admin_owner_tenant(getattr(ctx, "user_id", None))


def _reply(ctx: CommandContext, args: list) -> str:
    from cli._admin_home import admin_data_dir
    from core.runtime_paths import cron_db_path
    from surfaces.telegram.job_ops import jobs_reply
    sub = args[0].lower() if args else "list"
    write = sub in _WRITES
    home = admin_data_dir(write=write)
    if not write and not os.path.exists(cron_db_path(home)):
        if sub not in ("list", "ls", "show"):
            from surfaces.telegram.job_ops import USAGE
            return USAGE
        from cron.service import scheduler_off_note
        note = scheduler_off_note()
        body = candy.empty("scheduled jobs", "add one with `/job new 30m <what>`")
        return f"{body}\n{candy.GUTTER}⚠ {note}" if note else body
    return jobs_reply(_tenant(ctx), home, args, via="repl")


def _emit(ctx: CommandContext, args: list) -> None:
    try:
        ctx.emit(_reply(ctx, args), title="jobs")
    except Exception as e:  # fail-open: a locked/absent store is never a crash
        ctx.emit(f"{candy.GUTTER}(scheduled jobs unavailable: {e})", title="jobs")


def h_jobs(ctx: CommandContext) -> None:
    """``/jobs`` — every live scheduled job."""
    _emit(ctx, ["list"])


def h_job(ctx: CommandContext) -> None:
    """``/job new|show|edit|pause|resume|run|stop|allow …``."""
    _emit(ctx, list(getattr(ctx, "args", None) or []))


HELP_JOB = (
    "  Scheduled jobs: work that runs at set times and survives a restart.\n"
    "  Each run is its own chat with Rob.\n"
    "\n"
    "    /jobs                          what is scheduled, next run, last outcome\n"
    "    /job new 30m <what>            every 30 minutes\n"
    "    /job new 'every monday 09:00' <what>   (or: every monday 09:00 | <what>)\n"
    "    /job new '0 9 * * *' <what>    5-field schedule line\n"
    "    /job new 1d <what> tools=defi_trade,defi_data target=0x… chain=base\n"
    "                                   grant tools; name the one token it may buy\n"
    "    /job show <id>                 one job and the instruction it runs\n"
    "    /job edit <id> schedule 2h     change one field; the rest is kept\n"
    "    /job edit <id> what <text>\n"
    "    /job pause <id> · /job resume <id>\n"
    "    /job run <id>                  run it on the next scheduler pass\n"
    "    /job stop <id>                 end it for good\n"
    "\n"
    "  A stored job only runs when the scheduler is on; if it is not, I say so\n"
    "  and name the one command that turns it on.",
    "`polyrob job` in a shell, `/jobs` on Telegram, and the console's Work.",
)
