"""`polyrob update` — check for and apply POLYROB updates.

``--check`` / ``--dry-run`` / ``--json`` report status; ``--apply`` performs the
automated snapshot → install → guarded-migrate → verify update with auto-rollback
(git/editable installs); ``--rollback`` / ``--list-snapshots`` manage the snapshot
safety net. Other install methods get honest per-method manual instructions.
"""
from __future__ import annotations

import json as _json
import os
import sys
import urllib.request

import click

from cli.update.context import resolve_update_context
from cli.update.detect import (
    DEFER_TO_MANAGER, DOCKER, EDITABLE_GIT, GIT, PIP, PIPX, SYSTEMD, UNKNOWN,
    detect_install,
)
from cli.update.engine import apply_update
from cli.update.process_guard import (
    UpdateLockHeld, active_use_reasons, update_lock,
)
from cli.update.runners import build_runners
from cli.update.snapshot import (
    latest_complete, list_snapshots, restore_snapshot,
)
from cli.update.versions import resolve_status

# Exit codes (CI-friendly): 0 up-to-date, 10 update available, 1 error.
EXIT_UP_TO_DATE = 0
EXIT_UPDATE_AVAILABLE = 10
EXIT_ERROR = 1

# Every code-update step also runs the DB migration (`migrate upgrade`) so a self-host
# following the printed instructions can't upgrade code past a schema change and hit
# "no such column" on an un-migrated DB. The server auto-migrates at boot (api/app.py),
# but the CLI/manual paths must migrate explicitly; the runner is idempotent (a no-op
# when already current).
_MIGRATE = "python -m migrations.migrate upgrade"
_MANUAL_STEPS = {
    EDITABLE_GIT: f"git pull --ff-only && pip install -e . && {_MIGRATE}",
    GIT: f"git pull --ff-only && pip install . && {_MIGRATE}",
    PIP: f'pip install -U "polyrob[all]" && {_MIGRATE}',
    PIPX: f"pipx upgrade polyrob && {_MIGRATE}",
    DOCKER: "docker compose pull && docker compose up -d --build",
    UNKNOWN: "update via the package manager you installed POLYROB with, "
             f"then run: {_MIGRATE}",
}


def _parse_unit_files(output: str) -> list:
    """Service names out of `systemctl list-unit-files 'polyrob*'` plain output."""
    units = []
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            units.append(parts[0])
    return units


def _detect_polyrob_units() -> list:
    """Best-effort: which polyrob* systemd units exist on this box. [] on any failure."""
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "list-unit-files", "polyrob*", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    return _parse_unit_files(out)


def _systemd_manual_steps(units: list) -> str:
    """Manual update steps for a systemd install, targeting the units that exist.

    Historically hardcoded `polyrob-api` — a unit that doesn't exist on the headless
    prod shape (`polyrob.service`), so following the steps never restarted the agent
    and old code kept running (U3, 2026-07-14 review). Always includes daemon-reload.
    """
    if units:
        names = " ".join(units)
        return (f"sudo systemctl stop {names} && git pull --ff-only && "
                f"pip install . && {_MIGRATE} && "
                f"sudo systemctl daemon-reload && sudo systemctl start {names}")
    # Couldn't detect the unit set — name both known shapes and say how to check.
    return ("sudo systemctl stop polyrob.service (headless) or polyrob-api.service "
            "(api shape) — check which exists: systemctl list-unit-files 'polyrob*' — "
            f"then: git pull --ff-only && pip install . && {_MIGRATE} && "
            "sudo systemctl daemon-reload && sudo systemctl start <that unit>")


def _manual_steps_for(method: str) -> str:
    if method == SYSTEMD:
        return _systemd_manual_steps(_detect_polyrob_units())
    return _MANUAL_STEPS.get(method, _MANUAL_STEPS[UNKNOWN])


def _http_get(url: str, timeout: float = 6.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "polyrob-update"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed hosts)
        return resp.read().decode("utf-8")


# POLYROB's canonical release channel is GitHub Releases/tags. git checkouts, editable
# installs, and systemd/server installs (deployed via git/rsync and updated with
# `git pull`) all track GitHub — none of them are PyPI wheels. Only true pip/pipx wheel
# installs track PyPI (and only once the package is published there). An UNKNOWN install
# defaults to the canonical GitHub channel. The old mapping sent SYSTEMD → pypi, so the
# prod/server path queried a channel POLYROB isn't published to and `--check` always said
# "could not check".
def _source_for(ctx) -> str:
    return "pypi" if ctx.method in {PIP, PIPX} else "github"


def _fmt_snapshot(info) -> str:
    m = info.manifest
    ver = (m.from_version if m else "?")
    scope = f"  [{m.scope}]" if m else ""
    tag = "" if info.complete else "  (INCOMPLETE)"
    return f"  {info.name}  v{ver}{scope}{tag}"


def _do_list_snapshots(as_json: bool) -> None:
    uctx = resolve_update_context()
    infos = list_snapshots(uctx.snapshots_root)
    if as_json:
        click.echo(_json.dumps([
            {"name": i.name, "complete": i.complete,
             "from_version": i.manifest.from_version if i.manifest else None,
             "created_at": i.manifest.created_at if i.manifest else None}
            for i in infos], indent=2))
        return
    if not infos:
        click.echo(f"No snapshots yet ({uctx.snapshots_root}).")
        return
    click.echo(f"Snapshots in {uctx.snapshots_root} (newest first):")
    for info in infos:
        click.echo(_fmt_snapshot(info))


def _rollback_db_targets(target) -> list:
    """DB paths a restore would overwrite (kind='db' items in the snapshot)."""
    m = target.manifest
    if not m:
        return []
    from pathlib import Path
    return [Path(i.original) for i in m.items if i.kind == "db"]


def _rollback_fail(as_json: bool, message: str) -> None:
    """Emit a rollback failure (JSON-aware) and exit non-zero."""
    if as_json:
        click.echo(_json.dumps({"rolled_back": False, "error": message}))
    else:
        click.echo(click.style(message, fg="red"))
    sys.exit(EXIT_ERROR)


def _do_rollback(snapshot_name: str, assume_yes: bool, as_json: bool,
                 force: bool = False) -> None:
    uctx = resolve_update_context()
    if snapshot_name:
        target = next((i for i in list_snapshots(uctx.snapshots_root)
                       if i.name == snapshot_name and i.complete), None)
        if target is None:
            _rollback_fail(as_json, f"No complete snapshot named '{snapshot_name}'.")
    else:
        target = latest_complete(uctx.snapshots_root)
        if target is None:
            _rollback_fail(as_json, "No complete snapshot to roll back to.")

    # Data-safety gate (§2.1): never os.replace / drop -wal underneath a live process
    # holding the DBs. Refuse unless --force. This catches an ACTIVE writer / running
    # REPL; it can't prove exclusivity, so --force stays available for the operator who
    # has verified nothing is running.
    reasons = active_use_reasons(_rollback_db_targets(target))
    if reasons and not force:
        if as_json:
            click.echo(_json.dumps(
                {"rolled_back": False, "error": "in_use", "reasons": list(reasons)}))
        else:
            click.echo(click.style(
                "Refusing to roll back: POLYROB appears to be in use.", fg="red"))
            for r in reasons:
                click.echo(f"  - {r}")
            click.echo("Stop the running server/REPL/agent first, then retry — or pass "
                       "--force to override (risks corrupting the DB under a live writer).")
        sys.exit(EXIT_ERROR)
    if reasons and force and not as_json:
        click.echo(click.style(
            "⚠ --force: restoring while POLYROB may be in use (DB corruption risk).",
            fg="yellow"))

    m = target.manifest
    if not as_json:
        click.echo(f"Rolling back to snapshot {target.name} (v{m.from_version if m else '?'}).")
        if m is not None and m.scope == "db_only":
            click.echo("This restores your databases (a db-only snapshot — config and "
                       "identity are NOT captured in it).")
        else:
            click.echo("This restores your databases, config, and identity to that snapshot.")
    if not assume_yes and not click.confirm("Proceed?", default=False):
        if as_json:
            click.echo(_json.dumps({"rolled_back": False, "reason": "aborted"}))
        else:
            click.echo("Aborted.")
        sys.exit(EXIT_UP_TO_DATE)
    try:
        with update_lock(uctx.snapshots_root):
            restored = restore_snapshot(target.path)
    except UpdateLockHeld as exc:
        _rollback_fail(as_json, f"Rollback failed: {exc}")
    except Exception as exc:  # torn snapshot / IO error
        _rollback_fail(as_json, f"Rollback failed: {exc}")
    n = len(restored.items)
    if as_json:
        click.echo(_json.dumps({
            "rolled_back": True, "snapshot": target.name,
            "from_version": m.from_version if m else None, "restored_items": n}))
    else:
        click.echo(click.style(f"✓ Restored {n} item(s) from {target.name}.", fg="green"))
    sys.exit(EXIT_UP_TO_DATE)


def _do_apply(channel: str, assume_yes: bool, force: bool, as_json: bool) -> None:
    """Automated apply: snapshot → install → guarded-migrate → verify → auto-rollback."""
    ctx = detect_install()
    status = resolve_status(channel=channel, fetch=_http_get, source=_source_for(ctx))
    if not status.update_available:
        msg = ("Already up to date." if status.latest is not None
               else f"Cannot apply: {status.human_note}.")
        click.echo(_json.dumps({"applied": False, "reason": "no_update", **status.as_dict()})
                   if as_json else msg)
        sys.exit(EXIT_UP_TO_DATE)

    # Tag-pinned channels move HEAD to the released tag; --channel git fast-forwards a
    # branch (target_ref=None). See cli/update/runners.py::build_runners.
    target_ref = status.latest if channel != "git" else None
    runners = build_runners(ctx, target_ref=target_ref)
    if runners is None:
        manual = _manual_steps_for(ctx.method)
        click.echo(click.style(
            f"Automated apply isn't supported for a {ctx.method} install. Update manually:",
            fg="cyan"))
        click.echo(f"  {manual}")
        sys.exit(EXIT_UP_TO_DATE)

    uctx = resolve_update_context()
    reasons = active_use_reasons(uctx.db_paths)
    if reasons and not force:
        click.echo(click.style("Refusing to apply: POLYROB appears to be in use.", fg="red"))
        for r in reasons:
            click.echo(f"  - {r}")
        click.echo("Stop the running server/REPL/agent first, then retry — or pass --force.")
        sys.exit(EXIT_ERROR)

    if not as_json:
        click.echo(f"Updating {status.current} → {status.latest} ({ctx.method}).")
        click.echo("Steps: snapshot → install → migrate (guarded) → verify → auto-rollback on failure.")
    if not assume_yes and not click.confirm("Proceed?", default=False):
        click.echo(_json.dumps({"applied": False, "reason": "aborted"})
                   if as_json else "Aborted.")
        sys.exit(EXIT_UP_TO_DATE)

    try:
        with update_lock(uctx.snapshots_root):
            res = apply_update(ctx=uctx, runners=runners,
                               from_version=status.current, to_version=status.latest or "")
    except UpdateLockHeld as exc:
        click.echo(_json.dumps({"applied": False, "error": str(exc)})
                   if as_json else click.style(f"Apply failed: {exc}", fg="red"))
        sys.exit(EXIT_ERROR)
    if res.ok:
        if as_json:
            click.echo(_json.dumps({
                "applied": True, "from_version": status.current,
                "to_version": status.latest, "snapshot": res.snapshot.name}))
        else:
            click.echo(click.style(
                f"✓ Updated to {status.latest}. (snapshot: {res.snapshot.name})", fg="green"))
        sys.exit(EXIT_UP_TO_DATE)
    if as_json:
        click.echo(_json.dumps({
            "applied": False, "failed_step": res.failed_step, "error": str(res.error),
            "snapshot": res.snapshot.name, "rolled_back": True}))
    else:
        click.echo(click.style(
            f"✗ Update failed at the '{res.failed_step}' step: {res.error}", fg="red"))
        click.echo(click.style(
            "Auto-rolled back — your databases, config, and code are back at "
            f"{status.current} (snapshot {res.snapshot.name}).", fg="yellow"))
    sys.exit(EXIT_ERROR)


@click.command("update")
@click.option("--check", "check_only", is_flag=True,
              help="Report current vs latest and exit (0 up-to-date, 10 if newer).")
@click.option("--dry-run", is_flag=True, help="Print the update plan without changing anything.")
@click.option("--channel", type=click.Choice(["stable", "pre", "git"]), default="stable",
              help="stable=latest release, pre=include prereleases, git=track branch.")
@click.option("--apply", "do_apply", is_flag=True,
              help="Automated apply: snapshot → install → guarded-migrate → verify → auto-rollback.")
@click.option("--rollback", "do_rollback", is_flag=True,
              help="Restore the most recent snapshot (databases, config, identity).")
@click.option("--snapshot", "snapshot_name", default="", metavar="NAME",
              help="With --rollback, restore this specific snapshot (see --list-snapshots).")
@click.option("--list-snapshots", "do_list", is_flag=True, help="List restorable snapshots.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Assume yes (non-interactive).")
@click.option("--force", is_flag=True,
              help="With --rollback or --apply, override the in-use guard (risks DB corruption).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def update_cmd(check_only: bool, dry_run: bool, channel: str, do_apply: bool,
               do_rollback: bool, snapshot_name: str, do_list: bool, assume_yes: bool,
               force: bool, as_json: bool):
    """Check for and apply POLYROB updates."""
    if do_list:
        _do_list_snapshots(as_json)
        sys.exit(EXIT_UP_TO_DATE)
    if do_rollback:
        _do_rollback(snapshot_name, assume_yes, as_json, force=force)
        return  # _do_rollback exits
    if do_apply:
        _do_apply(channel, assume_yes, force, as_json)
        return  # _do_apply exits

    ctx = detect_install()
    source = _source_for(ctx)
    status = resolve_status(channel=channel, fetch=_http_get, source=source)
    manual = _manual_steps_for(ctx.method)

    # U10: surface the DB-schema-vs-code state alongside the version check —
    # "code updated, DB never migrated" is exactly the failure this command
    # exists to prevent. Fail-open: never let the probe break `update`.
    try:
        from cli.commands.doctor import schema_status_line
        schema_line = schema_status_line(dict(os.environ))
    except Exception:
        schema_line = None

    if as_json:
        payload = {**status.as_dict(), "method": ctx.method,
                   "self_updatable": ctx.self_updatable, "manual_steps": manual}
        if schema_line is not None:
            payload["db_schema"] = schema_line
        click.echo(_json.dumps(payload, indent=2))
    else:
        click.echo(f"Install method : {ctx.method} ({ctx.reason})")
        click.echo(f"Current version: {status.current}")
        if status.latest is None:
            click.echo(f"Latest version : {status.human_note}")
        else:
            click.echo(f"Latest version : {status.latest} [{channel}]")
        if schema_line is not None:
            click.echo(schema_line)
        if status.update_available:
            click.echo(click.style("→ An update is available.", fg="yellow"))
        elif status.latest is not None:
            click.echo(click.style("✓ You are up to date.", fg="green"))

    # --check: pure status, CI exit code.
    if check_only:
        sys.exit(EXIT_UPDATE_AVAILABLE if status.update_available else EXIT_UP_TO_DATE)

    # Point at the automated path where it exists (git/editable installs have real
    # runners); everything else gets honest per-method manual steps.
    if not as_json:
        if ctx.method in DEFER_TO_MANAGER:
            click.echo(click.style(
                f"\nAutomated update is not available for a {ctx.method} install.", fg="cyan"))
        elif ctx.method in (GIT, EDITABLE_GIT):
            if dry_run:
                click.echo("\nPlan (dry-run): snapshot → install → migrate (guarded) → "
                           "verify → auto-rollback on failure")
                click.echo("Run `polyrob update --apply` to perform it.")
            else:
                click.echo(click.style(
                    "\nRun `polyrob update --apply` for the automated update "
                    "(snapshot → install → migrate → verify, auto-rollback on failure) "
                    "— or update manually:", fg="cyan"))
        else:
            click.echo(click.style(
                f"\nAutomated apply isn't supported for a {ctx.method} install yet. "
                "Update manually:", fg="cyan"))
        click.echo(f"  {manual}")
    sys.exit(EXIT_UP_TO_DATE)


update_cmd_export = update_cmd
