"""`polyrob update` — check for and apply POLYROB updates.

``--check`` / ``--dry-run`` / ``--json`` report status; ``--apply`` performs the
automated snapshot → install → migrate → verify update with auto-rollback
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

# Schema migrations apply automatically at the next start (027 WP2: the server
# at boot — api/app.py — AND the CLI container). The explicit runner remains for
# git checkouts (`python -m migrations.migrate upgrade`, idempotent); for a
# pip/pipx install the module now ships in the wheel, but prescribing an extra
# interpreter-sensitive command a fresh user can get wrong is worse than
# "migrations run on next start".
_MIGRATE = "python -m migrations.migrate upgrade"
_AUTO_MIGRATE_NOTE = "(schema migrations apply automatically on the next start)"
# `{spec}` / `{extras}` are filled per install by `_manual_steps_for`: the
# extras THIS install has (058 — a bare `pip install -e .` / `pip install -U
# polyrob` drops them; pipx keeps the spec it was installed with).
_HASHED_DEPS = ("python core/lock_closure.py deps --lock requirements.lock --pyproject pyproject.toml "
                "--extras \"{extras_csv}\"{packs_flag} -o polyrob-deps.txt && "
                "pip install --require-hashes -r polyrob-deps.txt")
_MANUAL_STEPS = {
    # 066 P1 / D2: hash-checked — the lock's closure of the extras, then the project --no-deps.
    # 067 (one install): {packs_flag} carries a retired separate pack dist's pack as
    # its SDK extra; {retire} then retires the old dist's METADATA (never pip
    # uninstall: it would delete pack files polyrob owns) after the project install.
    EDITABLE_GIT: (f"git pull --ff-only && {_HASHED_DEPS} && "
                   f"pip install --no-deps --no-build-isolation -e .{{retire}} && {_MIGRATE}"),
    GIT: (f"git pull --ff-only && {_HASHED_DEPS} && "
          f"pip install --no-deps --no-build-isolation .{{retire}} && {_MIGRATE}"),
    # A pack dist is NEVER appended here: an unrestricted `pip install -U` of a
    # pack name fetches whatever the index holds, which replaces reviewed code
    # (a retired first-party name may be anyone's there; a third-party pack came
    # from a pinned commit). {pack_note} names each pack's reviewed path instead.
    PIP: f"python -m pip install -U {{spec}}  {_AUTO_MIGRATE_NOTE}{{pack_note}}",
    PIPX: f"pipx upgrade polyrob  {_AUTO_MIGRATE_NOTE}",
    DOCKER: "docker compose pull && docker compose up -d --build",
    UNKNOWN: "update via the package manager you installed POLYROB with "
             f"{_AUTO_MIGRATE_NOTE}",
}


def _parse_unit_files(output: str) -> list:
    """Service names out of `systemctl list-unit-files 'polyrob*'` plain output.

    A bare TEMPLATE ("polyrob@.service") is skipped: `systemctl stop
    polyrob@.service` is invalid (no instance name), and since the manual steps
    are one `&&` chain, including it would abort the chain BEFORE `git pull` —
    the update would silently do nothing (the exact U3 failure class this
    function exists to prevent). Live instances ("polyrob@rob.service") pass.
    """
    units = []
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service") and not parts[0].endswith("@.service"):
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
    # A deployed /opt/polyrob is an rsync TARGET with no .git: `git pull` there
    # fails. The update path is the on-box deployer from the maintenance clone
    # (it quiesces the family, snapshots the venv, migrates, restarts, verifies
    # and rolls back), or a fresh `pip install` for a wheel-shaped install.
    if units:
        names = " ".join(units)
        return ("cd <maintenance clone> && git pull --ff-only && bash scripts/deploy_prod.sh   "
                f"(stops/starts {names}, migrates, verifies, auto-rolls back) — or, for a "
                f"wheel install: sudo systemctl stop {names} && pip install -U polyrob && "
                f"{_MIGRATE} && sudo systemctl daemon-reload && sudo systemctl start {names}")
    # Couldn't detect the unit set — name both known shapes and say how to check.
    return ("check which units exist (polyrob.service = headless agent, "
            "polyrob-x402-api.service = api): systemctl list-unit-files 'polyrob*' — then "
            "cd <maintenance clone> && git pull --ff-only && bash scripts/deploy_prod.sh, or for a "
            f"wheel install: stop them && pip install -U polyrob && {_MIGRATE} && "
            "sudo systemctl daemon-reload && sudo systemctl start <that unit>")


def _manual_steps_for(method: str, repo_root=None) -> str:
    if method == SYSTEMD:
        return _systemd_manual_steps(_detect_polyrob_units())
    try:
        from cli.update.extras import installed_extras, upgrade_spec
        extras = installed_extras(repo_root)
    except Exception:  # detection is advice, never a blocker
        extras = []
    try:
        from cli.update import packs as pk
        installed = pk.installed_packs()
        retired = sorted({p.dist for p in pk.retired_installed(installed)})
        pack_ids = sorted({p.id for p in pk.retired_installed(installed)})
    except Exception:  # detection is advice, never a blocker
        installed, retired, pack_ids = [], [], []
    bracket = f"[{','.join(extras)}]" if extras else ""
    return _MANUAL_STEPS.get(method, _MANUAL_STEPS[UNKNOWN]).format(
        spec=upgrade_spec(extras) if extras else "polyrob", extras=bracket,
        extras_csv=",".join(extras), packs_flag=f" --packs {','.join(pack_ids)}" if pack_ids else "",
        retire=" && python core/packs/retire.py" if retired else "",
        pack_note=_wheel_pack_note(installed) if method == PIP else "")


def _wheel_pack_note(installed) -> str:
    """How a wheel install updates each installed pack — never through the
    index (Codex 067 follow-up #1). A first-party pack ships inside polyrob
    (067, one install): `pip install -U polyrob[...]` updates it, nothing to
    add. A retired separate pack dist is uninstalled. Anything else is
    third-party: re-install from a pinned source. An unreadable index treats
    every pack as third-party (fail closed)."""
    if not installed:
        return ""
    try:
        from core.packs.index import first_party_identities
        first = {k: v[0] for k, v in first_party_identities().items()}
    except Exception:  # noqa: BLE001 — an unreadable index vouches for nothing
        first = {}
    from cli.update.packs import retired_installed
    retired = retired_installed(installed)
    fp = [p for p in installed if first.get(p.id) == p.dist]
    tp = [p for p in installed if p not in fp and p not in retired]
    parts = []
    if retired:
        names = " ".join(sorted({p.dist for p in retired}))
        parts.append(f"retired pack distribution(s) {names}: the packs ship inside polyrob "
                     f"now — after the upgrade run python -m core.packs.retire (never pip "
                     f"uninstall them: it deletes pack files polyrob owns)")
    for p in tp:
        parts.append(f"third-party pack {p.id} ({p.dist}): never auto-upgraded; re-install "
                     f"from a pinned source: polyrob pack install <source>@<sha>")
    if not parts:
        return ""
    return " ; packs are not upgraded by that command — " + " ; ".join(parts)


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
    if as_json and not assume_yes:
        click.echo(_json.dumps({"rolled_back": False, "reason": "confirmation_required",
                               "hint": "pass --yes with --json"}))
        sys.exit(EXIT_ERROR)
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


def _git_channel_status(ctx):
    """``--channel git`` measures the BRANCH against its upstream, not the release list."""
    from cli.update.runners import git_branch_status
    from cli.update.versions import UpdateStatus, installed_version
    st = git_branch_status(ctx)
    cur = installed_version()
    if not st["ok"]:
        return UpdateStatus(current=cur, latest=None, channel="git", error=st["reason"],
                            source_ref="the tracked branch"), st
    latest = f"{cur}+git.{st['upstream']}" if st["behind"] else cur
    return UpdateStatus(current=cur, latest=latest, channel="git",
                        source_ref=f"branch {st['branch']}"), st


def _do_apply(channel: str, assume_yes: bool, force: bool, as_json: bool,
              wait_idle: float = 1800.0) -> None:
    """Automated apply: wait for the wrap-up → snapshot → install → migrate →
    verify → auto-rollback."""
    ctx = detect_install()
    git_state = None
    if channel == "git":
        status, git_state = _git_channel_status(ctx)
    else:
        status = resolve_status(channel=channel, fetch=_http_get, source=_source_for(ctx))
    if status.error is not None or status.latest is None:
        msg = f"Cannot apply: {status.human_note}."
        click.echo(_json.dumps({"applied": False, "reason": "check_failed",
                               **status.as_dict()}) if as_json else msg)
        sys.exit(EXIT_ERROR)
    behind_branch = bool(git_state and git_state.get("behind"))
    if not status.update_available and not behind_branch:
        msg = "Already up to date."
        click.echo(_json.dumps({"applied": False, "reason": "no_update", **status.as_dict()})
                   if as_json else msg)
        sys.exit(EXIT_UP_TO_DATE)

    # Tag-pinned channels move HEAD to the released tag; --channel git fast-forwards a
    # branch (target_ref=None). See cli/update/runners.py::build_runners.
    target_ref = status.latest if channel != "git" else None
    runners = build_runners(ctx, target_ref=target_ref)
    if runners is None:
        manual = _manual_steps_for(ctx.method, ctx.repo_root)
        if as_json:
            click.echo(_json.dumps({"applied": False, "reason": "unsupported_method",
                                   "method": ctx.method, "manual_steps": manual}))
            sys.exit(EXIT_ERROR)
        click.echo(click.style(
            f"Automated apply isn't supported for a {ctx.method} install. Update manually:",
            fg="cyan"))
        click.echo(f"  {manual}")
        sys.exit(EXIT_ERROR)

    uctx = resolve_update_context()
    # 058: perceive the agent's state and wait for the wrap-up. A live turn, a
    # running goal or a cron rail (or one due imminently) is a reason to WAIT,
    # not to give up — the same gate scripts/deploy_when_idle.sh applies before
    # a prod deploy. An unreadable store is busy. A resident server PROCESS is a
    # different fact (waiting never ends it) and is refused below as before.
    from cli.update.agent_state import observe, wait_for_wrap_up
    activity = observe(uctx.data_home)
    if not activity.idle and wait_idle > 0:
        if not as_json:
            click.echo(click.style(f"POLYROB is busy: {activity.describe()}.", fg="yellow"))
            click.echo(f"Waiting for the wrap-up (up to {int(wait_idle)} s; --no-wait to refuse instead)…")
        last = [""]

        def _tick(act, waited):
            if as_json:
                return
            text = act.describe()
            if text != last[0]:
                click.echo(f"  … still busy after {int(waited)} s: {text}")
                last[0] = text

        activity = wait_for_wrap_up(uctx.data_home, timeout=wait_idle, on_tick=_tick)
        if activity.idle and not as_json:
            click.echo(click.style(f"Agent idle after {int(activity.waited)} s — continuing.", fg="green"))
    if not activity.idle and not force:
        if as_json:
            click.echo(_json.dumps({"applied": False, "error": "in_use",
                                   "reasons": list(activity.reasons),
                                   "unreadable": list(activity.unreadable),
                                   "waited_sec": int(activity.waited)}))
            sys.exit(EXIT_ERROR)
        click.echo(click.style(
            f"Refusing to apply: POLYROB is still busy after {int(activity.waited)} s.", fg="red"))
        click.echo(f"  - {activity.describe()}")
        click.echo("Retry later, raise --wait-idle, or pass --force to swap code under it anyway.")
        sys.exit(EXIT_ERROR)
    reasons = active_use_reasons(uctx.db_paths)
    if reasons and not force:
        if as_json:
            click.echo(_json.dumps({"applied": False, "error": "in_use",
                                   "reasons": list(reasons)}))
            sys.exit(EXIT_ERROR)
        click.echo(click.style("Refusing to apply: POLYROB appears to be in use.", fg="red"))
        for r in reasons:
            click.echo(f"  - {r}")
        click.echo("Stop the running server/REPL/agent first, then retry — or pass --force.")
        sys.exit(EXIT_ERROR)

    if not as_json:
        click.echo(f"Updating {status.current} → {status.latest} ({ctx.method}).")
        click.echo("Steps: snapshot → install → migrate → verify → auto-rollback on failure.")
    if as_json and not assume_yes:
        click.echo(_json.dumps({"applied": False, "reason": "confirmation_required",
                               "hint": "pass --yes with --json"}))
        sys.exit(EXIT_ERROR)
    if not assume_yes and not click.confirm("Proceed?", default=False):
        click.echo("Aborted.")
        sys.exit(EXIT_UP_TO_DATE)

    try:
        with update_lock(uctx.snapshots_root):
            res = apply_update(ctx=uctx, runners=runners,
                               from_version=status.current, to_version=status.latest or "")
    except Exception as exc:
        click.echo(_json.dumps({"applied": False, "error": str(exc)})
                   if as_json else click.style(f"Apply failed: {exc}", fg="red"))
        sys.exit(EXIT_ERROR)
    if res.ok:
        # Every apply writes a full snapshot (every DB + wallet/ + identity/ + every
        # .env). Without pruning they accumulate forever under <data_home>/snapshots/.
        try:
            from cli.update.snapshot import prune_snapshots
            prune_snapshots(uctx.snapshots_root, keep=3)
        except Exception:
            pass  # pruning is housekeeping; never fail a completed update over it
        if as_json:
            click.echo(_json.dumps({
                "applied": True, "from_version": status.current,
                "to_version": status.latest, "snapshot": res.snapshot.name}))
        else:
            click.echo(click.style(
                f"✓ Updated to {status.latest}. (snapshot: {res.snapshot.name})", fg="green"))
            for line in _restart_hints():
                click.echo(line)
        sys.exit(EXIT_UP_TO_DATE)
    if as_json:
        click.echo(_json.dumps({
            "applied": False, "failed_step": res.failed_step, "error": str(res.error),
            "snapshot": res.snapshot.name, "rolled_back": res.rolled_back,
            "rollback_errors": list(res.rollback_errors)}))
    else:
        click.echo(click.style(
            f"✗ Update failed at the '{res.failed_step}' step: {res.error}", fg="red"))
        if res.rolled_back:
            click.echo(click.style(
                "Auto-rolled back databases, config, and code to "
                f"{status.current} (snapshot {res.snapshot.name}).", fg="yellow"))
        else:
            click.echo(click.style(
                f"Rollback incomplete; retain snapshot {res.snapshot.name} for recovery.",
                fg="red"))
            for error in res.rollback_errors:
                click.echo(f"  {error}")
    sys.exit(EXIT_ERROR)


def _restart_hints() -> list:
    """After a successful update, name what is still running the OLD code.

    062: `polyrob service install` can now leave a background agent running,
    and an update that rewrites the code under a live process changes nothing
    until it restarts. Saying "updated" without saying that is the kind of
    half-true report this repo keeps paying for.
    """
    out = []
    try:
        from cli.commands.service import (LAUNCHD_LABEL, UNIT_NAME,
                                          _launchd_plist_path, _platform,
                                          _systemd_unit_path)
        plat = _platform()
        if plat == "linux" and _systemd_unit_path().is_file():
            out.append(f"  Restart the background service:  systemctl --user restart {UNIT_NAME}")
        elif plat == "macos" and _launchd_plist_path().is_file():
            out.append(f"  Restart the background service:  launchctl kickstart -k "
                       f"gui/$(id -u)/{LAUNCHD_LABEL}")
    except Exception:
        pass
    try:
        units = _detect_polyrob_units()
        if units:
            out.append("  Restart the system units:  sudo systemctl restart "
                       + " ".join(units))
    except Exception:
        pass
    out.append("  A running REPL or chat surface keeps the OLD code until it restarts.")
    return out


@click.command("update")
@click.option("--check", "check_only", is_flag=True,
              help="Report current vs latest and exit (0 up-to-date, 10 newer, 1 unknown/error).")
@click.option("--dry-run", is_flag=True, help="Print the update plan without changing anything.")
@click.option("--channel", type=click.Choice(["stable", "pre", "git"]), default="stable",
              help="stable=latest release, pre=include prereleases, git=track branch.")
@click.option("--apply", "do_apply", is_flag=True,
              help="Automated apply: snapshot → install → migrate → verify → auto-rollback.")
@click.option("--rollback", "do_rollback", is_flag=True,
              help="Restore the most recent snapshot (databases, config, identity).")
@click.option("--snapshot", "snapshot_name", default="", metavar="NAME",
              help="With --rollback, restore this specific snapshot (see --list-snapshots).")
@click.option("--list-snapshots", "do_list", is_flag=True, help="List restorable snapshots.")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Assume yes (non-interactive).")
@click.option("--force", is_flag=True,
              help="With --rollback or --apply, override the in-use guard (risks DB corruption).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--wait-idle", "wait_idle", type=float, default=1800.0, metavar="SECONDS",
              help="With --apply: wait up to this long for a live turn / goal / cron job "
                   "to wrap up before swapping code (default 1800).")
@click.option("--no-wait", "no_wait", is_flag=True,
              help="With --apply: refuse immediately when the agent is busy instead of waiting.")
def update_cmd(check_only: bool, dry_run: bool, channel: str, do_apply: bool,
               do_rollback: bool, snapshot_name: str, do_list: bool, assume_yes: bool,
               force: bool, as_json: bool, wait_idle: float, no_wait: bool):
    """Check for and apply POLYROB updates."""
    if sum((check_only, dry_run, do_apply, do_rollback, do_list)) > 1:
        message = "Choose only one of --check, --dry-run, --apply, --rollback, --list-snapshots."
        if as_json:
            click.echo(_json.dumps({"error": message}))
            sys.exit(EXIT_ERROR)
        raise click.UsageError(message)
    if snapshot_name and not do_rollback:
        raise click.UsageError("--snapshot requires --rollback.")
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    if do_list:
        _do_list_snapshots(as_json)
        sys.exit(EXIT_UP_TO_DATE)
    if do_rollback:
        _do_rollback(snapshot_name, assume_yes, as_json, force=force)
        return  # _do_rollback exits
    if do_apply:
        _do_apply(channel, assume_yes, force, as_json, wait_idle=0.0 if no_wait else wait_idle)
        return  # _do_apply exits

    ctx = detect_install()
    source = _source_for(ctx)
    status = resolve_status(channel=channel, fetch=_http_get, source=source)
    manual = _manual_steps_for(ctx.method, ctx.repo_root)

    # U10: surface the DB-schema-vs-code state alongside the version check —
    # "code updated, DB never migrated" is exactly the failure this command
    # exists to prevent. Fail-open: never let the probe break `update`.
    try:
        from cli.commands.doctor import schema_status_line
        schema_line = schema_status_line(dict(os.environ))
    except Exception:
        schema_line = None

    # 067 (one install): a leftover retired separate pack distribution.
    try:
        from cli.update.packs import retired_installed, retired_note
        pack_lines = retired_note()
        retired_dists = sorted({p.dist for p in retired_installed()})
    except Exception:  # advice, never a blocker
        pack_lines, retired_dists = [], []

    if as_json:
        payload = {**status.as_dict(), "method": ctx.method,
                   "self_updatable": ctx.self_updatable, "manual_steps": manual,
                   "retired_pack_dists": retired_dists}
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
        for line in pack_lines:
            click.echo(click.style(line, fg="yellow"))

    # --check: pure status, CI exit code.
    if check_only:
        if status.error is not None or status.latest is None:
            sys.exit(EXIT_ERROR)
        sys.exit(EXIT_UPDATE_AVAILABLE if status.update_available else EXIT_UP_TO_DATE)

    # Point at the automated path where it exists (git/editable installs have real
    # runners); everything else gets honest per-method manual steps.
    if not as_json:
        if ctx.method in DEFER_TO_MANAGER:
            click.echo(click.style(
                f"\nAutomated update is not available for a {ctx.method} install.", fg="cyan"))
        elif ctx.method in (GIT, EDITABLE_GIT):
            if dry_run:
                click.echo("\nPlan (dry-run): wait for the agent to wrap up → snapshot → "
                           "install → migrate → verify → auto-rollback on failure")
                click.echo("Run `polyrob update --apply` to perform it.")
            else:
                click.echo(click.style(
                    "\nRun `polyrob update --apply` for the automated update "
                    "(waits for the agent to wrap up, then snapshot → install → migrate → "
                    "verify, auto-rollback on failure) "
                    "— or update manually:", fg="cyan"))
        else:
            click.echo(click.style(
                f"\nAutomated apply isn't supported for a {ctx.method} install yet. "
                "Update manually:", fg="cyan"))
        click.echo(f"  {manual}")
    sys.exit(EXIT_UP_TO_DATE)


update_cmd_export = update_cmd
