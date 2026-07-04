"""``/cron`` slash-command handler — read-only list of scheduled cron jobs.

A terminal-native window onto the durable cron store (roadmap P5). This handler
is deliberately read-only: scheduling/cancel already exist via the agent-facing
``cronjob`` tool (``tools/cronjob_tools.py``) and ``polyrob`` click surface, so the
REPL command only *reports* the current schedule.

Resolution mirrors the sibling ``/goals`` handler (``_h_goals``): the cron DB path
is ``<get_data_root()>/cron.db`` so this reads the SAME database the autonomy
dispatcher/lifespan ticker writes, and jobs are listed via the same
``CronService(CronJobStore(...)).list_jobs(user_id=...)`` seam used by
``autonomy_status_lines``. Fail-open per the existing handler contract — a missing
store / disabled cron degrades to a friendly one-liner, never a REPL teardown.

The main session wires registration (``handlers.py`` / ``registry.py``); this file
only defines the handler.
"""

from __future__ import annotations

from cli.ui.commands.registry import CommandContext


def h_cron(ctx: CommandContext) -> None:
    """List this user's scheduled cron jobs (read-only).

    ``/cron`` or ``/cron list`` → one compact line per job (short id, status,
    schedule spec, next-run time, task preview). No jobs / disabled / missing DB
    → a friendly one-liner. Scoped to ``ctx.user_id or "local"``.
    """
    user_id = ctx.user_id or "local"
    try:
        from pathlib import Path

        from core.runtime_config import get_data_root
        from cron.jobs import CronJobStore
        from cron.service import CronService

        db_path = Path(get_data_root()) / "cron.db"
        if not db_path.exists():
            ctx.emit("Cron not enabled (no cron jobs scheduled).", title="cron")
            return

        service = CronService(CronJobStore(str(db_path)))
        jobs = service.list_jobs(user_id=user_id)

        if not jobs:
            ctx.emit("No cron jobs scheduled.", title="cron")
            return

        lines = [f"Cron jobs ({len(jobs)}):"]
        for job in jobs[:20]:
            when = job.next_run_at.isoformat() if job.next_run_at else "-"
            state = job.status if job.enabled else f"{job.status} (disabled)"
            task_preview = (job.task or "").replace("\n", " ")[:48]
            lines.append(
                f"  - {job.id[:8]} [{state}] {job.schedule_spec} -> {when}: {task_preview}"
            )
        if len(jobs) > 20:
            lines.append(f"  ... (+{len(jobs) - 20} more)")

        ctx.emit("\n".join(lines), title="cron")
    except Exception as e:  # fail-open: store may not exist / be locked yet
        ctx.emit(f"Cron: (unavailable: {e})", title="cron")
