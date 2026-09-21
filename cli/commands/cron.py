"""`polyrob cron` — owner management of durable scheduled agent runs.

Closes parity gap G4 (2026-07-12 UI-surface review): cron jobs could not be
created or cancelled from any human surface — REPL ``/cron``, webview
``/autonomy`` and Telegram ``/status`` are read-only, so scheduling was
agent-tool-only. This group rides the SAME ``cron.service.CronService`` +
``cron.db`` the lifespan ticker (``core/autonomy_runtime.py``) and the webview
``/api/webgate/cron`` endpoint use, at the ADMIN data home
(``cli/_admin_home.py::admin_data_dir`` — the 031 rule, which adopts the
DEPLOYED home when the shell declares none) — a job scheduled here is exactly
what the ticker will run.

Schedule specs (``cron/schedule.py``): a duration (``30m``), ``every monday
09:00``, a 5-field cron line, or an ISO one-shot timestamp.
"""
from __future__ import annotations

from typing import Optional

import click

from cli._admin_home import as_root_option


def _service(*, write: "bool | None" = None):
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService

    from cli._admin_home import admin_data_dir

    setup_project_path()
    setup_sqlite_compat()
    return CronService(CronJobStore(cron_db_path(admin_data_dir(write=write))))


def _tenant(user: Optional[str]) -> str:
    """The tenant this verb acts on — the ONE owner resolver.

    C24: this read ``core.identity.resolve_identity()``, which resolves from the
    SHELL's environment. On a deployed box, where systemd exports the owner
    binding and an SSH shell carries none, ``polyrob cron list`` therefore
    listed tenant ``local`` and reported a confident "no cron jobs" over the
    rails the service was running under its own tenant.
    ``admin_owner_principal`` adopts the deployment's declaration, exactly as
    ``admin_data_dir`` adopts its data home.
    """
    if user:
        return user
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _resolve_job_id(svc, job_id: str, tenant: str) -> str:
    """A full id, or a UNIQUE prefix of one, tenant-scoped.

    `cron list` prints 8-char ids, so that is what an operator types back;
    `edit <8 chars>` used to fail with a message that blamed the rig (maint,
    2026-09-21 00:50Z). Exact match wins; an ambiguous prefix names the
    candidates; no match is "no job".
    """
    job_id = job_id.strip()
    if svc.store.get(job_id, user_id=tenant) is not None:
        return job_id
    hits = [j for j in svc.list_jobs(user_id=tenant) if j.id.startswith(job_id)]
    if len(hits) == 1:
        return hits[0].id
    if len(hits) > 1:
        raise click.ClickException(
            f"ambiguous job id {job_id!r} — matches: " + ", ".join(sorted(j.id for j in hits)))
    raise click.ClickException(f"no job {job_id!r} for this tenant")


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
@as_root_option
def schedule(task: str, schedule_spec: str, user: Optional[str], max_duration: int):
    """Schedule TASK on SCHEDULE_SPEC (e.g. '30m', 'every monday 09:00')."""
    from cron.schedule import ScheduleError

    svc = _service(write=True)
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
@as_root_option
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

    svc = _service(write=True)
    tenant = _tenant(user)
    existing = _digest_jobs(svc, tenant)
    if off:
        if not existing:
            from cli.ui.candy import empty
            click.echo(empty("digest job scheduled", "nothing to cancel", yet=False))
            return
        for job in existing:
            svc.cancel(job.id, user_id=tenant)
        click.echo(f"cancelled {len(existing)} digest job(s)")
        return
    if not schedule_spec:
        if not existing:
            from cli.ui.candy import empty
            click.echo(empty("digest job scheduled",
                             "schedule one:  polyrob cron digest 'every day 08:00'"))
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
              help="List jobs across all tenants AND include cancelled rows")
@click.option("--cancelled", "show_cancelled", is_flag=True, default=False,
              help="Include cancelled jobs (hidden by default).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def list_jobs(user: Optional[str], all_tenants: bool, show_cancelled: bool,
              as_json: bool):
    """List cron jobs (newest schedule first).

    057 WS-C (B12): cancelled rows are HIDDEN unless you ask for them. The store
    is used as a config editor — prod carried ~70 cancelled rows, several of them
    same-day duplicates of a live rail — and a list that leads with dead rows is
    how a near-duplicate gets recreated instead of edited.
    """
    svc = _service(write=False)
    jobs = svc.list_jobs(user_id=None if all_tenants else _tenant(user))
    hidden = 0
    if not (show_cancelled or all_tenants):
        live = [j for j in jobs if j.status != "cancelled"]
        hidden = len(jobs) - len(live)
        jobs = live
    if as_json:
        import json
        from dataclasses import asdict
        click.echo(json.dumps([asdict(j) for j in jobs], indent=2, default=str))
        return
    if not jobs:
        # C53: this said "no cron jobs" and then, one line later, that N
        # cancelled jobs were hidden — two statements about the same store that
        # cannot both be true. One sentence, and it names what it left out.
        from cli.ui.candy import empty
        if hidden:
            click.echo(empty("live cron jobs",
                             f"{hidden} cancelled row(s) are hidden — "
                             f"--cancelled to show them", yet=False))
        else:
            click.echo(empty("cron jobs", "nothing is scheduled for this tenant"))
        return
    for job in jobs:
        click.echo(_fmt(job))
    if hidden:
        click.echo(click.style(
            f"  ({hidden} cancelled job(s) hidden — --cancelled to show, "
            f"'polyrob cron prune' to remove)", dim=True))


def _parse_age_days(spec: str) -> float:
    """``7d`` / ``48h`` / a bare number of days -> days. Refuses anything else,
    rather than silently pruning on a value it guessed."""
    raw = (spec or "").strip().lower()
    try:
        if raw.endswith("d"):
            return float(raw[:-1])
        if raw.endswith("h"):
            return float(raw[:-1]) / 24.0
        return float(raw)
    except ValueError:
        raise click.ClickException(
            f"unreadable age {spec!r} — use 7d, 48h or a number of days")


@cron.command("prune")
@click.option("--cancelled-older-than", "older_than", default="7d",
              help="Age cutoff for cancelled rows (e.g. 7d, 48h). Default 7d.")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@click.option("--all", "all_tenants", is_flag=True, default=False,
              help="Prune across all tenants")
@click.option("--dry-run", is_flag=True, default=False,
              help="Say what would be removed without removing it.")
@as_root_option
def prune(older_than: str, user: Optional[str], all_tenants: bool, dry_run: bool):
    """Delete CANCELLED cron jobs older than a cutoff (057 WS-C B12).

    Only ``cancelled`` rows are touched — a live, failed or completed job is
    never removed by this verb.
    """
    days = _parse_age_days(older_than)
    svc = _service(write=not dry_run)
    tenant = None if all_tenants else _tenant(user)
    from datetime import datetime, timedelta
    # C54: the preview built its cutoff from one `datetime.now()` and the DELETE
    # built a second one milliseconds later inside the store, so a row on the
    # boundary could be listed and not removed (or removed and not listed). ONE
    # clock, passed to both.
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    doomed = [j for j in svc.list_jobs(user_id=tenant)
              if j.status == "cancelled" and j.created_at is not None
              and j.created_at < cutoff]
    if not doomed:
        from cli.ui.candy import empty
        click.echo(empty("cancelled job older than " + older_than,
                         "nothing to prune", yet=False))
        return
    # The number that decides is the store's OWN predicate (dry_run counts the
    # rows the very same WHERE clause would delete); the listing above is the
    # human view of it.
    would = svc.store.prune_cancelled(older_than_days=days, user_id=tenant,
                                      now=now, dry_run=True)
    if dry_run:
        for j in doomed:
            click.echo(_fmt(j))
        click.echo(f"would remove {would} cancelled job(s)")
        if would != len(doomed):
            click.echo(click.style(
                f"  ⚠ the listing shows {len(doomed)} — a row with no "
                f"recorded creation time is prunable but not listable.",
                fg="yellow"))
        return
    removed = svc.store.prune_cancelled(older_than_days=days, user_id=tenant,
                                        now=now)
    click.echo(f"pruned {removed} cancelled job(s) older than {older_than}")
    if removed != would:
        click.echo(click.style(
            f"  ⚠ the store counted {would} a moment earlier — another writer "
            f"changed the table between the count and the delete.", fg="yellow"))


@cron.command("show")
@click.argument("job_id")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
def show(job_id: str, user: Optional[str]):
    """Show one job in full (schedule, status, payload, timestamps)."""
    svc = _service(write=False)
    job_id = _resolve_job_id(svc, job_id, _tenant(user))
    job = svc.store.get(job_id, user_id=_tenant(user))
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
              help="056 WS5: priority class in payload.priority — orders jobs within a tick.")
@click.option("--preempts/--no-preempts", "preempts", default=None,
              help="057 WS-C: may this job PRE-EMPT a running board goal? "
                   "Unset = the money class decides (back-compat).")
@click.option("--rig", default=None,
              help="057 WS-A: named tool rig in payload.rig — money_rail|social|research|ops|full "
                   "(or 'none' to clear it). A narrow rig ships far fewer tool schemas per step.")
@click.option("--deliver", default=None,
              help="Out-of-band delivery target in payload.deliver — telegram|email|twitter|… "
                   "(or 'none' to DROP it: the run then reports only through its own "
                   "message()/receipts, never a done() echo to the owner).")
@click.option("--task-file", type=click.Path(exists=True, dir_okay=False, readable=True),
              default=None,
              help="Replace the job's task PROSE with this file's text (schedule, next run, cap "
                   "and payload untouched). The alternative was cancel + re-schedule, which loses "
                   "last_run_at and every payload edit.")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@as_root_option
def edit(job_id: str, max_duration: Optional[int], schedule: Optional[str],
         priority: Optional[str], preempts: Optional[bool], rig: Optional[str],
         deliver: Optional[str], task_file: Optional[str], user: Optional[str]):
    """Change a job's cap, schedule, priority, pre-emption and/or rig (tenant-scoped).

    Why: the EXIT/SCOUT treasury rails were created with a 240 s cap and timed
    out on 22 of 24 runs (2026-09-17) — until now the only remedy was a raw
    sqlite UPDATE on the live db. The ceiling is 1800 s: the hourly buyback rail
    (reconcile → quote → gates → swap → ledger → report) runs 1-5 min per step
    and was cut at step 6 by the old 600 s ceiling before it could swap.
    """
    svc = _service(write=True)
    tenant = _tenant(user)
    if max_duration is None and schedule is None and priority is None \
            and rig is None and preempts is None and deliver is None and task_file is None:
        raise click.ClickException(
            "nothing to change: pass --max-duration, --schedule, --priority, "
            "--preempts, --rig, --deliver and/or --task-file")
    job_id = _resolve_job_id(svc, job_id, _tenant(user))
    if max_duration is not None:
        if svc.store.set_max_duration(job_id, max_duration, user_id=tenant):
            click.echo(f"{job_id}: max duration → {max_duration}s (from the next run)")
        else:
            raise click.ClickException(f"no job {job_id!r} for this tenant")
    if schedule is not None:
        if svc.store.set_schedule(job_id, schedule, user_id=tenant):
            # C55: `svc.store.get(job_id)` carried no tenant, so the line that
            # CONFIRMS the change read a row this tenant may not own.
            _row = svc.store.get(job_id, user_id=tenant)
            _next = _row.next_run_at if _row is not None else "unknown"
            click.echo(f"{job_id}: schedule → {schedule!r}; next run {_next}")
        else:
            raise click.ClickException(f"could not re-time {job_id!r}: bad spec or no job for this tenant")
    if priority is not None:
        if svc.store.set_priority(job_id, priority, user_id=tenant):
            click.echo(f"{job_id}: priority → {priority}")
        else:
            raise click.ClickException(f"could not set priority on {job_id!r}")
    if preempts is not None:
        # 057 WS-C (B7): priority ORDERS jobs within a tick; preempts INTERRUPTS
        # work already running. Prod's SAFETY and WATCHER rails are read-only and
        # money-class, so they pre-empted a running goal for nothing.
        if svc.store.set_preempts(job_id, preempts, user_id=tenant):
            click.echo(f"{job_id}: preempts → {str(bool(preempts)).lower()}")
        else:
            raise click.ClickException(f"could not set preempts on {job_id!r}")
    if rig is not None:
        from cron.rig_edit import set_job_rig
        from core.config_policy.rigs import is_rig, rig_names
        _clear = rig.strip().lower() in ("none", "", "-")
        if not _clear and not is_rig(rig):
            # The job was already resolved above, so a refusal here is the RIG.
            raise click.ClickException(
                f"unknown rig {rig!r} (valid rigs: {', '.join(rig_names())}, "
                f"or 'none' to clear)")
        if set_job_rig(svc.store, job_id, None if _clear else rig, user_id=_tenant(user)):
            click.echo(f"{job_id}: rig → {'(cleared)' if _clear else rig.strip().lower()}")
        else:
            raise click.ClickException(f"could not set rig on {job_id!r} (no job for this tenant)")
    if deliver is not None:
        from cron.delivery import ALLOWED_TARGETS
        from cron.rig_edit import set_job_deliver
        _drop = deliver.strip().lower() in ("none", "", "-")
        if not _drop and deliver.strip().lower() not in ALLOWED_TARGETS:
            raise click.ClickException(
                f"unknown delivery target {deliver!r} (valid: {', '.join(ALLOWED_TARGETS)}, "
                f"or 'none' to drop it)")
        if set_job_deliver(svc.store, job_id, None if _drop else deliver, user_id=_tenant(user)):
            click.echo(f"{job_id}: deliver → {'(dropped)' if _drop else deliver.strip().lower()}")
        else:
            raise click.ClickException(f"could not set deliver on {job_id!r} (no job for this tenant)")
    if task_file is not None:
        with open(task_file, "r", encoding="utf-8") as fh:
            text = fh.read()
        if not text.strip():
            raise click.ClickException(f"{task_file}: empty — a job with no task is a cancel, not an edit")
        if svc.store.set_task(job_id, text, user_id=_tenant(user)):
            click.echo(f"{job_id}: task → {len(text.strip())} chars from {task_file}")
        else:
            raise click.ClickException(f"could not set task on {job_id!r} (no job for this tenant)")


@cron.command("cancel")
@click.argument("job_id")
@click.option("--user", default=None, help="Tenant id (default: this instance's identity)")
@as_root_option
def cancel(job_id: str, user: Optional[str]):
    """Cancel a job (tenant-scoped — you can only cancel your own)."""
    svc = _service(write=True)
    job_id = _resolve_job_id(svc, job_id, _tenant(user))
    if svc.cancel(job_id, user_id=_tenant(user)):
        click.echo(f"cancelled {job_id}")
    else:
        raise click.ClickException(f"no job {job_id!r} for this tenant")
