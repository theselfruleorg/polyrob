"""`polyrob cron` — owner management of durable scheduled agent runs.

Closes parity gap G4 (2026-07-12 UI-surface review): cron jobs could not be
created or cancelled from any human surface — REPL ``/cron``, webview
``/autonomy`` and Telegram ``/status`` are read-only, so scheduling was
agent-tool-only. This group rides the SAME ``cron.service.CronService`` +
``cron.db`` the lifespan ticker (``core/autonomy_runtime.py``) and the webview
``/api/webgate/cron`` endpoint use, at the shared data home
(``core.runtime_paths.resolve_data_home``) — a job scheduled here is exactly
what the ticker will run.

Schedule specs (``cron/schedule.py``): a duration (``30m``), ``every monday
09:00``, a 5-field cron line, or an ISO one-shot timestamp.
"""
from __future__ import annotations

from typing import Optional

import click


def _service():
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    from core.runtime_paths import resolve_data_home
    from cron.jobs import CronJobStore
    from cron.service import CronService

    setup_project_path()
    setup_sqlite_compat()
    return CronService(CronJobStore(str(resolve_data_home() / "cron.db")))


def _tenant(user: Optional[str]) -> str:
    from core.identity import resolve_identity
    return (user or resolve_identity() or "").strip() or "local"


def _warn_if_cron_off() -> None:
    """A stored job only runs if the ticker is on — never mislead the owner."""
    try:
        from tools.cronjob_tools import cron_enabled
        enabled = cron_enabled()
    except Exception:
        enabled = False
    if not enabled:
        click.echo(click.style(
            "note: CRON_ENABLED is off — the job is stored but no ticker will "
            "run it until you enable cron (CRON_ENABLED=true or "
            "AUTONOMY_POSTURE=full).", fg="yellow"))


def _fmt(job) -> str:
    status_color = {"scheduled": "green", "running": "yellow", "done": "blue",
                    "failed": "red", "cancelled": "white"}.get(job.status, "white")
    nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
    shot = "once" if job.one_shot else "recurring"
    return (f"{click.style(job.status.ljust(10), fg=status_color)} "
            f"{job.id}  [{job.schedule_spec} · {shot} · next {nxt}]  {job.task}")


@click.group("cron")
def cron():
    """Schedule, inspect and cancel durable cron jobs."""


@cron.command("schedule")
@click.argument("task")
@click.argument("schedule_spec")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@click.option("--max-duration", default=180, type=int,
              help="Per-run hard cap in seconds (default 180)")
def schedule(task: str, schedule_spec: str, user: Optional[str], max_duration: int):
    """Schedule TASK on SCHEDULE_SPEC (e.g. '30m', 'every monday 09:00')."""
    from cron.schedule import ScheduleError

    svc = _service()
    try:
        job = svc.schedule(task=task, schedule_spec=schedule_spec,
                           user_id=_tenant(user), max_duration_seconds=max_duration)
    except ScheduleError as e:
        raise click.ClickException(f"invalid schedule: {e}")
    nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
    click.echo(f"scheduled {click.style(job.id, bold=True)} — next run {nxt}")
    _warn_if_cron_off()


@cron.command("list")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@click.option("--all", "all_tenants", is_flag=True, default=False,
              help="List jobs across all tenants")
def list_jobs(user: Optional[str], all_tenants: bool):
    """List cron jobs (newest schedule first)."""
    svc = _service()
    jobs = svc.list_jobs(user_id=None if all_tenants else _tenant(user))
    if not jobs:
        click.echo(click.style("no cron jobs", dim=True))
        return
    for job in jobs:
        click.echo(_fmt(job))


@cron.command("show")
@click.argument("job_id")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
def show(job_id: str, user: Optional[str]):
    """Show one job in full (schedule, status, payload, timestamps)."""
    svc = _service()
    job = svc.store.get(job_id, user_id=_tenant(user))
    if job is None:
        raise click.ClickException(f"no job {job_id!r} for this tenant")
    click.echo(_fmt(job))
    click.echo(f"  user:         {job.user_id}")
    click.echo(f"  created:      {job.created_at}")
    click.echo(f"  last run:     {job.last_run_at or '-'}")
    click.echo(f"  max duration: {job.max_duration_seconds}s")
    if job.payload:
        click.echo(f"  payload:      {job.payload}")


@cron.command("cancel")
@click.argument("job_id")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
def cancel(job_id: str, user: Optional[str]):
    """Cancel a job (tenant-scoped — you can only cancel your own)."""
    svc = _service()
    if svc.cancel(job_id, user_id=_tenant(user)):
        click.echo(f"cancelled {job_id}")
    else:
        raise click.ClickException(f"no job {job_id!r} for this tenant")
