"""`polyrob update` — check for and (soon) apply POLYROB updates.

This slice ships the read-only, zero-mutation surface: ``--check`` / ``--dry-run`` /
``--json`` plus honest per-install-method manual instructions. The snapshot/rollback
safety net and the automated apply paths land in subsequent slices (see
docs/plans/2026-07-01-polyrob-update-command-UPGRADE-PROPOSAL.md).
"""
from __future__ import annotations

import json as _json
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
    SYSTEMD: "sudo systemctl stop polyrob-api && git pull --ff-only && "
             f"pip install . && {_MIGRATE} && "
             "sudo systemctl start polyrob-api",
    DOCKER: "docker compose pull && docker compose up -d --build",
    UNKNOWN: "update via the package manager you installed POLYROB with, "
             f"then run: {_MIGRATE}",
}


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
    tag = "" if info.complete else "  (INCOMPLETE)"
    return f"  {info.name}  v{ver}{tag}"


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
        manual = _MANUAL_STEPS.get(ctx.method, _MANUAL_STEPS[UNKNOWN])
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
    manual = _MANUAL_STEPS.get(ctx.method, _MANUAL_STEPS[UNKNOWN])

    if as_json:
        payload = {**status.as_dict(), "method": ctx.method,
                   "self_updatable": ctx.self_updatable, "manual_steps": manual}
        click.echo(_json.dumps(payload, indent=2))
    else:
        click.echo(f"Install method : {ctx.method} ({ctx.reason})")
        click.echo(f"Current version: {status.current}")
        if status.latest is None:
            click.echo(f"Latest version : {status.human_note}")
        else:
            click.echo(f"Latest version : {status.latest} [{channel}]")
        if status.update_available:
            click.echo(click.style("→ An update is available.", fg="yellow"))
        elif status.latest is not None:
            click.echo(click.style("✓ You are up to date.", fg="green"))

    # --check: pure status, CI exit code.
    if check_only:
        sys.exit(EXIT_UPDATE_AVAILABLE if status.update_available else EXIT_UP_TO_DATE)

    # Apply path is not automated yet — be honest and give exact manual steps.
    if not as_json:
        if ctx.method in DEFER_TO_MANAGER:
            click.echo(click.style(
                f"\nAutomated update is not available for a {ctx.method} install.", fg="cyan"))
        elif dry_run:
            click.echo("\nPlan (dry-run): backup → fetch → install → migrate → verify → rollback-on-failure")
            click.echo("Automated apply is not wired in this build yet.")
        else:
            click.echo(click.style(
                "\nAutomated apply is coming in a later build. For now, update manually:", fg="cyan"))
        click.echo(f"  {manual}")
    sys.exit(EXIT_UP_TO_DATE)


update_cmd_export = update_cmd
