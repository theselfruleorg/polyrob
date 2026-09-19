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
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService

    from cli._admin_home import admin_data_dir

    setup_project_path()
    setup_sqlite_compat()
    return CronService(CronJobStore(cron_db_path(admin_data_dir())))


def _tenant(user: Optional[str]) -> str:
    from core.identity import resolve_identity
    return (user or resolve_identity() or "").strip() or "local"


def _warn_if_cron_off() -> None:
    """A stored job only runs if the ticker is on — never mislead the owner."""
    from cli._flag_warn import warn_if_flag_off

    def _enabled() -> bool:
        from tools.cronjob_tools import cron_enabled
        return cron_enabled()

    warn_if_flag_off(
        "CRON_ENABLED",
        "the job is stored but no ticker will run it.",
        enabled_fn=_enabled,
        remedy="polyrob config set CRON_ENABLED true --global "
               "(or AUTONOMY_POSTURE=full)",
    )


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
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@cron.command("schedule")
@click.argument("task")
@click.argument("schedule_spec")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@click.option("--max-duration", default=600, type=int,
              help="Per-run hard cap in seconds (default 600)")
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


#: The digest job's task string is a MARKER, never a prompt — `cron/runner.py`
#: routes `payload.digest` to `cron/digest.py`, which composes the message
#: deterministically from the ledger, the event log and the open asks. No model
#: call, no cost.
_DIGEST_TASK = "[owner-daily-digest]"


def _digest_jobs(svc, tenant: str) -> list:
    return [j for j in svc.list_jobs(user_id=tenant)
            if (j.payload or {}).get("digest") and j.status != "cancelled"]


@cron.command("digest")
@click.argument("schedule_spec", required=False)
@click.option("--off", is_flag=True, default=False, help="Cancel the digest job.")
@click.option("--deliver", default="telegram", help="Surface to deliver on (default telegram).")
@click.option("--days", default=1, type=int, help="Days of activity to summarize.")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
def digest(schedule_spec: Optional[str], off: bool, deliver: str, days: int,
           user: Optional[str]):
    """Schedule the owner daily digest on SCHEDULE_SPEC (e.g. 'every day 08:00').

    The digest is the roll-up for everything the delivery rail could not send
    you live. `OWNER_DIGEST_ENABLED` turning it on is not enough — a job has to
    run it, and until this verb existed the only way to create one was a script
    that is not part of the published package.

    Calling it again MOVES the schedule rather than adding a second digest.
    """
    from cron.schedule import ScheduleError

    svc = _service()
    tenant = _tenant(user)
    existing = _digest_jobs(svc, tenant)
    if off:
        if not existing:
            click.echo(click.style("no digest job scheduled", dim=True))
            return
        for job in existing:
            svc.cancel(job.id, user_id=tenant)
        click.echo(f"cancelled {len(existing)} digest job(s)")
        return
    if not schedule_spec:
        if not existing:
            click.echo(click.style("no digest job scheduled", dim=True))
            click.echo("schedule one:  polyrob cron digest 'every day 08:00'")
            return
        for job in existing:
            click.echo(_fmt(job))
        return
    try:
        job = svc.schedule(
            task=_DIGEST_TASK, schedule_spec=schedule_spec, user_id=tenant,
            payload={"digest": True, "wake_agent": False,
                     "deliver": deliver, "days": max(1, int(days))})
    except ScheduleError as e:
        raise click.ClickException(f"invalid schedule: {e}")
    # AFTER the new one is persisted: a failed reschedule must not leave the
    # owner with no digest at all.
    for old in existing:
        svc.cancel(old.id, user_id=tenant)
    nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
    click.echo(f"digest scheduled {click.style(job.id, bold=True)} — next run {nxt}")
    _warn_if_digest_off()
    _warn_if_cron_off()


def _warn_if_digest_off() -> None:
    """A scheduled job that the runtime will skip is worth one line now."""
    try:
        from core.config_policy import AutonomyConfig
        from core.env import bool_env
        if not AutonomyConfig.owner_digest_enabled():
            click.echo(click.style(
                "  note: OWNER_DIGEST_ENABLED is off — the job will not compose",
                fg="yellow"))
        if not bool_env("CRON_DELIVERY_ENABLED", False):
            click.echo(click.style(
                "  note: CRON_DELIVERY_ENABLED is off — the digest will not be sent",
                fg="yellow"))
    except Exception:
        click.echo(click.style("  note: could not read the digest flags", dim=True))


@cron.command("list")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@click.option("--all", "all_tenants", is_flag=True, default=False,
              help="List jobs across all tenants")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def list_jobs(user: Optional[str], all_tenants: bool, as_json: bool):
    """List cron jobs (newest schedule first)."""
    svc = _service()
    jobs = svc.list_jobs(user_id=None if all_tenants else _tenant(user))
    if as_json:
        import json
        from dataclasses import asdict
        click.echo(json.dumps([asdict(j) for j in jobs], indent=2, default=str))
        return
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


@cron.command("edit")
@click.argument("job_id")
@click.option("--max-duration", type=click.IntRange(1, 1800), default=None,
              help="New hard cap in seconds (1-1800, the same ceiling the agent tool has).")
@click.option("--schedule", default=None,
              help="056 WS5: new schedule spec (5-field cron / 'every …' / duration); next run recomputed from now.")
@click.option("--priority", type=click.Choice(["money", "ops"]), default=None,
              help="056 WS5: priority class in payload.priority — 'money' rails pre-empt a running board goal.")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
def edit(job_id: str, max_duration: Optional[int], schedule: Optional[str],
         priority: Optional[str], user: Optional[str]):
    """Change a job's hard cap, schedule and/or priority class (tenant-scoped).

    Why: the EXIT/SCOUT treasury rails were created with a 240 s cap and timed
    out on 22 of 24 runs (2026-09-17) — until now the only remedy was a raw
    sqlite UPDATE on the live db. The ceiling is 1800 s: the hourly buyback rail
    (reconcile → quote → gates → swap → ledger → report) runs 1-5 min per step
    and was cut at step 6 by the old 600 s ceiling before it could swap.
    """
    svc = _service()
    if max_duration is None and schedule is None and priority is None:
        raise click.ClickException("nothing to change: pass --max-duration, --schedule and/or --priority")
    if max_duration is not None:
        if svc.store.set_max_duration(job_id, max_duration, user_id=_tenant(user)):
            click.echo(f"{job_id}: max duration → {max_duration}s (from the next run)")
        else:
            raise click.ClickException(f"no job {job_id!r} for this tenant")
    if schedule is not None:
        if svc.store.set_schedule(job_id, schedule, user_id=_tenant(user)):
            click.echo(f"{job_id}: schedule → {schedule!r}; next run {svc.store.get(job_id).next_run_at}")
        else:
            raise click.ClickException(f"could not re-time {job_id!r}: bad spec or no job for this tenant")
    if priority is not None:
        if svc.store.set_priority(job_id, priority, user_id=_tenant(user)):
            click.echo(f"{job_id}: priority → {priority}")
        else:
            raise click.ClickException(f"could not set priority on {job_id!r}")


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
