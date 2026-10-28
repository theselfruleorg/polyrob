"""`polyrob service` — run the agent in the background on THIS machine.

The tree had three systemd writers before 062 (the prod deploy units, the
``polyrob@`` profile template, ``polyrob browser install``) and none of them
answered the owner's own question: keep my agent running after I close the
terminal. This one does, for the two platforms a self-hoster actually uses.

Design notes that matter:

* **A user service, never a system one.** The agent holds the owner's keys and
  its data home is under ``$HOME``; a root unit would run it as the wrong user
  and a system unit would need a data home outside the home directory.
* **A user unit inherits nothing.** systemd ``--user`` exports no shell
  environment, so the unit pins ``POLYROB_HOME`` (and the profile, when one is
  active) explicitly. Leaving that implicit is exactly how ``polyrob owner
  pending`` came to print three different answers depending on how it was
  started.
* **It runs the gateway**, so every enabled surface starts from one unit.
* **077 — a drained restart.** ``restart`` (and ``/restart`` in chat, and
  ``update --apply``) asks the RUNNING agent to finish its work and exit with
  75; the unit's ``RestartForceExitStatus=75`` starts it again and the new
  process reports "back online". ``stop`` lets the live turn finish (SIGTERM is
  a bounded drain). Root and ``sudo -u`` shells get the user bus filled in
  (``_service_ops``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

from core.lifecycle import LAUNCHD_LABEL  # one label: the plist and supervisor()

UNIT_NAME = "polyrob.service"


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / UNIT_NAME


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _active_profile() -> str:
    return (os.environ.get("POLYROB_PROFILE") or "").strip()


def _polyrob_home() -> str:
    from core.paths import polyrob_home
    return str(polyrob_home())


def _exec_argv() -> list[str]:
    """The command the service runs: this interpreter, the module entry point.

    ``sys.executable`` is the venv the user installed into — the same
    interpreter the ``polyrob`` shim execs — so the unit can never drift onto a
    different checkout the way a bare ``polyrob`` on ``PATH`` can.

    ``-I`` (isolated mode): ``-m`` otherwise puts the working directory first on
    ``sys.path``, so a ``cli/`` or ``core/`` package in it would run in place of
    polyrob's own (SUP-1) — the same flag the install.sh shim uses.
    """
    argv = [sys.executable, "-I", "-m", "cli.polyrob"]
    profile = _active_profile()
    if profile:
        argv += ["-P", profile]
    argv.append("gateway")
    return argv


def _run(cmd: list[str], check: bool = False):
    if cmd[:2] == ["systemctl", "--user"]:
        # 077 F7: `sudo -u <user>` has no XDG_RUNTIME_DIR — fill in the user bus.
        from cli.commands._service_ops import user_bus_env
        return subprocess.run(cmd, capture_output=True, text=True, check=check,
                              env=user_bus_env())
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _configured_env() -> dict:
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    return dict(os.environ)


def _user_option(f):
    return click.option(
        "--user", "as_user", default=None, metavar="NAME",
        help="As root: act on this user's service (default: the one user that has it).")(f)


def _delegated(as_user) -> bool:
    """Root with no unit of its own: re-run this verb as the service's user."""
    from cli.commands._service_ops import delegate_as_service_user

    def _fail(msg):
        raise click.ClickException(msg)

    code = delegate_as_service_user(as_user, echo=click.echo, fail=_fail,
                                    unit_path=_systemd_unit_path())
    if code is None:
        return False
    if code:
        raise SystemExit(code)
    return True


@click.group("service", invoke_without_command=True)
@click.pass_context
def service(ctx):
    """Run the agent in the background (systemd user unit / launchd agent)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    if ctx.invoked_subcommand is None:
        ctx.invoke(service_status)


@service.command("needed", hidden=True)
def service_needed():
    """Exit 0 when a chat surface is configured (install.sh asks this)."""
    from cli.surfaces_config import gateway_surfaces
    surfaces = gateway_surfaces(_configured_env())
    if not surfaces:
        raise SystemExit(1)
    click.echo(", ".join(surfaces))


def _write_systemd_unit() -> Path:
    unit = _systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(_quote(a) for a in _exec_argv())
    profile = _active_profile()
    env_lines = [f'Environment="POLYROB_HOME={_polyrob_home()}"']
    if profile:
        env_lines.append(f'Environment="POLYROB_PROFILE={profile}"')
    unit.write_text(
        "[Unit]\n"
        "Description=POLYROB agent (chat surfaces + autonomy loops)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{chr(10).join(env_lines)}\n"
        f"WorkingDirectory={Path.home()}\n"
        f"ExecStart={exec_line}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "# 077: exit 75 = a drained restart the owner asked for: start again, never a failure.\n"
        "RestartForceExitStatus=75\n"
        "SuccessExitStatus=75\n"
        "# SIGTERM lets the live turn finish (up to 60 s) before the process stops.\n"
        "TimeoutStopSec=120\n"
        "KillMode=mixed\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    return unit


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _write_launchd_plist() -> Path:
    plist = _launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    args = "".join(f"    <string>{a}</string>\n" for a in _exec_argv())
    env = f"    <key>POLYROB_HOME</key><string>{_polyrob_home()}</string>\n"
    profile = _active_profile()
    if profile:
        env += f"    <key>POLYROB_PROFILE</key><string>{profile}</string>\n"
    logs = Path(_polyrob_home()) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n" + args + "  </array>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n" + env + "  </dict>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "  <key>KeepAlive</key><true/>\n"
        "  <key>ExitTimeOut</key><integer>120</integer>\n"
        f"  <key>StandardOutPath</key><string>{logs / 'service.log'}</string>\n"
        f"  <key>StandardErrorPath</key><string>{logs / 'service.err'}</string>\n"
        "</dict>\n</plist>\n",
        encoding="utf-8",
    )
    return plist


@service.command("install")
@click.option("--start/--no-start", default=True, show_default=True,
              help="Start the service immediately after installing it.")
def service_install(start: bool):
    """Install (and start) the background service."""
    plat = _platform()
    if plat == "unsupported":
        raise click.ClickException(
            f"no background service for this platform ({sys.platform}). "
            "Run `polyrob gateway` yourself, or use your own supervisor.")

    from cli.surfaces_config import describe_surfaces, gateway_surfaces
    env = _configured_env()
    running = gateway_surfaces(env)
    if not running:
        described = describe_surfaces(env)
        detail = f" Configured but not startable: {', '.join(described)}." if described else ""
        click.echo("⚠ No chat surface is enabled, so the service would idle."
                   f"{detail}\n  Set one up first: polyrob setup")

    if plat == "linux":
        if not shutil.which("systemctl"):
            raise click.ClickException(
                "systemctl not found. Write your own supervisor entry for: "
                + " ".join(_exec_argv()))
        unit = _write_systemd_unit()
        click.echo(f"Wrote {unit}")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", UNIT_NAME])
        if start:
            res = _run(["systemctl", "--user", "restart", UNIT_NAME])
            if res.returncode != 0:
                raise click.ClickException(
                    f"could not start {UNIT_NAME}: {(res.stderr or '').strip()[:300]}\n"
                    f"  Logs: journalctl --user -u {UNIT_NAME} -n 50")
        click.echo(f"Enabled {UNIT_NAME} (systemd --user).")
        click.echo("  Keep it running after you log out:  loginctl enable-linger $USER")
        click.echo(f"  Logs:  journalctl --user -u {UNIT_NAME} -f")
    else:
        plist = _write_launchd_plist()
        click.echo(f"Wrote {plist}")
        uid = os.getuid()
        _run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
        res = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        if res.returncode != 0:
            # Older macOS releases only speak load/unload.
            res = _run(["launchctl", "load", "-w", str(plist)])
        if res.returncode != 0:
            raise click.ClickException(
                f"could not load {LAUNCHD_LABEL}: {(res.stderr or '').strip()[:300]}")
        click.echo(f"Loaded {LAUNCHD_LABEL} (launchd).")
        click.echo(f"  Logs:  tail -f {Path(_polyrob_home()) / 'logs' / 'service.log'}")


@service.command("status")
@_user_option
def service_status(as_user=None):
    """Is the background service installed and running — and on which version?"""
    if _delegated(as_user):
        return
    plat = _platform()
    from cli.surfaces_config import describe_surfaces
    env = _configured_env()
    surfaces = describe_surfaces(env) or ["none configured"]

    if plat == "linux":
        unit = _systemd_unit_path()
        if not unit.is_file():
            click.echo("service: not installed (`polyrob service install`)")
        else:
            state = _run(["systemctl", "--user", "is-active", UNIT_NAME]).stdout.strip()
            click.echo(f"service: {state or 'unknown'} ({unit})")
    elif plat == "macos":
        plist = _launchd_plist_path()
        if not plist.is_file():
            click.echo("service: not installed (`polyrob service install`)")
        else:
            res = _run(["launchctl", "list", LAUNCHD_LABEL])
            state = "loaded" if res.returncode == 0 else "not loaded"
            click.echo(f"service: {state} ({plist})")
    else:
        click.echo(f"service: unsupported platform ({sys.platform})")

    # A server runs polyrob* SYSTEM units, which this verb does not own but
    # must not pretend are absent.
    try:
        from cli.commands.update import _detect_polyrob_units
        system_units = _detect_polyrob_units()
    except Exception:
        system_units = []
    if system_units:
        click.echo("system units (not managed here): " + ", ".join(system_units))

    click.echo(f"surfaces: {', '.join(surfaces)}")
    click.echo(f"command:  {' '.join(_exec_argv())}")
    for line in lifecycle_lines():
        click.echo(line)


def lifecycle_lines() -> list:
    """077: what is RUNNING vs what is on disk, an open restart request, the
    last restart's outcome and the settings that wait for a restart. Read-only;
    an unreadable record is said, never shown as empty."""
    import time as _time
    from core import lifecycle as lc
    out = []
    try:
        home = _data_home()
    except Exception as exc:  # noqa: BLE001 — a status line, never a crash
        return [f"lifecycle: data home unresolved ({exc})"]
    disk = lc.version_label(lc.code_version(), lc.code_sha())
    try:
        boot = lc.read_boot(data_home=home)
    except lc.LifecycleUnreadable as exc:
        boot = None
        out.append(f"running:  unreadable boot record ({exc})")
    if boot:
        pid = int(boot.get("pid") or 0)
        alive = False
        try:
            os.kill(pid, 0)
            alive = True
        except PermissionError:
            alive = True
        except (OSError, ValueError):
            alive = False
        running = lc.version_label(boot.get("version"), boot.get("sha"))
        if alive:
            up = int(_time.time() - float(boot.get("started_at") or _time.time()))
            health = "healthy" if boot.get("healthy_at") else "starting"
            out.append(f"running:  {running} — pid {pid}, up {up // 3600}h{(up % 3600) // 60:02d}m, "
                       f"{health}")
            if running != disk:
                out.append(f"on disk:  {disk} — the running agent is older; "
                           "`polyrob service restart` starts the new code")
        else:
            out.append(f"last run: {running} (pid {pid}, not running)")
    try:
        req = lc.read_request(data_home=home)
        if req is not None:
            out.append(f"request:  {req.describe()} — {req.state}")
    except lc.LifecycleUnreadable as exc:
        out.append(f"request:  unreadable ({exc})")
    try:
        last = lc.history(limit=1, data_home=home)
        if last:
            when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(float(last[0].get("at") or 0)))
            out.append(f"last:     {last[0].get('outcome')} at {when} — "
                       f"{str(last[0].get('detail') or '')[:160]}")
    except lc.LifecycleUnreadable as exc:
        out.append(f"last:     unreadable ({exc})")
    try:
        keys = lc.pending_keys(data_home=home)
        if keys:
            out.append(f"waiting:  {len(keys)} setting(s) apply at the next restart: "
                       + ", ".join(keys[:8]) + (" …" if len(keys) > 8 else ""))
    except lc.LifecycleUnreadable as exc:
        out.append(f"waiting:  unreadable ({exc})")
    return out


@service.command("uninstall")
def service_uninstall():
    """Stop and remove the background service (data is untouched)."""
    plat = _platform()
    if plat == "linux":
        unit = _systemd_unit_path()
        if not unit.is_file():
            click.echo("service: not installed (nothing to remove)")
            return
        _run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        unit.unlink()
        click.echo(f"Removed {unit}")
        _run(["systemctl", "--user", "daemon-reload"])
    elif plat == "macos":
        plist = _launchd_plist_path()
        if not plist.is_file():
            click.echo("service: not installed (nothing to remove)")
            return
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        _run(["launchctl", "unload", str(plist)])
        plist.unlink()
        click.echo(f"Removed {plist}")
    else:
        raise click.ClickException(f"nothing to remove on {sys.platform}")
    # Say what was NOT touched only when something actually was.
    click.echo("Service removed. Your data is untouched.")


# --------------------------------------------------------------------------
# 077 — start / stop / restart / logs
# --------------------------------------------------------------------------

def _require_installed() -> str:
    plat = _platform()
    if plat == "linux":
        if not shutil.which("systemctl"):
            raise click.ClickException("systemctl not found on this machine")
        if not _systemd_unit_path().is_file():
            _system_units_hint()
            raise click.ClickException("service: not installed (`polyrob service install`)")
    elif plat == "macos":
        if not _launchd_plist_path().is_file():
            raise click.ClickException("service: not installed (`polyrob service install`)")
    else:
        raise click.ClickException(f"no background service on this platform ({sys.platform})")
    return plat


def _system_units_hint() -> None:
    try:
        from cli.commands.update import _detect_polyrob_units
        units = _detect_polyrob_units()
    except Exception:
        units = []
    if units:
        click.echo("This box runs POLYROB as system units (" + ", ".join(units) + "). "
                   "Restart them with `sudo systemctl restart <unit>` (the agent finishes its "
                   "live turn first), or send /restart from your chat.")


def _active(plat: str) -> bool:
    if plat == "linux":
        return _run(["systemctl", "--user", "is-active", UNIT_NAME]).stdout.strip() == "active"
    res = _run(["launchctl", "list", LAUNCHD_LABEL])
    return res.returncode == 0 and '"PID"' in (res.stdout or "")


def _main_pid(plat: str) -> int:
    try:
        if plat == "linux":
            out = _run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                        UNIT_NAME]).stdout.strip()
            return int(out or 0)
        for line in (_run(["launchctl", "list", LAUNCHD_LABEL]).stdout or "").splitlines():
            if '"PID"' in line:
                return int(line.split("=")[1].strip().rstrip(";"))
    except (ValueError, IndexError):
        pass
    return 0


def _supervisor_start(plat: str) -> None:
    if plat == "linux":
        res = _run(["systemctl", "--user", "start", UNIT_NAME])
    else:
        uid = os.getuid()
        res = _run(["launchctl", "bootstrap", f"gui/{uid}", str(_launchd_plist_path())])
        if res.returncode != 0:
            res = _run(["launchctl", "kickstart", f"gui/{uid}/{LAUNCHD_LABEL}"])
    if res.returncode != 0:
        from cli.commands._service_ops import bus_error_hint
        raise click.ClickException(f"could not start the service: "
                                   f"{(res.stderr or '').strip()[:300]}"
                                   + bus_error_hint(res.stderr))


def _supervisor_restart(plat: str) -> None:
    """The supervisor's own restart: SIGTERM (a bounded drain), then start."""
    if plat == "linux":
        res = _run(["systemctl", "--user", "restart", UNIT_NAME])
    else:
        res = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
    if res.returncode != 0:
        from cli.commands._service_ops import bus_error_hint
        raise click.ClickException(f"could not restart the service: "
                                   f"{(res.stderr or '').strip()[:300]}"
                                   + bus_error_hint(res.stderr))


def _data_home():
    from cli._admin_home import admin_data_dir
    return Path(admin_data_dir(write=None))


def _report_boot(data_home, after: float, timeout: float = 300.0) -> None:
    from cli.commands._service_ops import wait_for_healthy_boot
    from core.lifecycle import version_label
    click.echo("  … waiting for the agent to come up")
    boot = wait_for_healthy_boot(data_home=data_home, after=after, timeout=timeout)
    if boot is None:
        raise click.ClickException(
            f"the service did not report a healthy start within {int(timeout)} s — "
            f"see `polyrob service logs`")
    took = int(float(boot.get("healthy_at") or 0) - after)
    click.echo(click.style(f"✓ Running {version_label(boot.get('version'), boot.get('sha'))} "
                           f"(pid {boot.get('pid')}, up after {max(took, 0)} s).", fg="green"))


@service.command("start")
@_user_option
def service_start(as_user):
    """Start the background service."""
    if _delegated(as_user):
        return
    import time as _time
    plat = _require_installed()
    if _active(plat):
        click.echo("service: already running (`polyrob service restart` to restart it)")
        return
    t0 = _time.time()
    _supervisor_start(plat)
    _report_boot(_data_home(), t0)


@service.command("stop")
@click.option("--now", is_flag=True,
              help="Stop at once, even in the middle of a turn (default: finish it, up to 60 s).")
@_user_option
def service_stop(now: bool, as_user):
    """Stop the background service (it finishes the live turn first)."""
    if _delegated(as_user):
        return
    plat = _require_installed()
    if not _active(plat):
        click.echo("service: not running")
        return
    if now and plat == "linux":
        _run(["systemctl", "--user", "kill", "--signal=SIGINT", UNIT_NAME])
    if plat == "linux":
        res = _run(["systemctl", "--user", "stop", UNIT_NAME])
    else:
        res = _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
    if res.returncode != 0:
        from cli.commands._service_ops import bus_error_hint
        raise click.ClickException(f"could not stop the service: "
                                   f"{(res.stderr or '').strip()[:300]}"
                                   + bus_error_hint(res.stderr))
    click.echo("service: stopped (`polyrob service start` to start it again)")


@service.command("restart")
@click.option("--wait-idle", "wait_idle", type=float, default=600.0, show_default=True,
              metavar="SECONDS",
              help="How long the agent may take to finish its current work before the "
                   "restart is called off.")
@click.option("--no-wait", "no_wait", is_flag=True,
              help="Call the restart off at once if the agent is busy.")
@click.option("--now", is_flag=True, help="Restart at once, even in the middle of a turn.")
@_user_option
def service_restart(wait_idle: float, no_wait: bool, now: bool, as_user):
    """Restart the agent once it finishes its current work, and report its version."""
    if _delegated(as_user):
        return
    import time as _time
    from core import lifecycle as lc
    from cli.commands._service_ops import wait_for_request
    plat = _require_installed()
    data_home = _data_home()
    t0 = _time.time()
    if not _active(plat):
        click.echo("service: not running — starting it")
        _supervisor_start(plat)
        _report_boot(data_home, t0)
        return
    try:
        boot = lc.read_boot(data_home=data_home)
    except lc.LifecycleUnreadable:
        boot = None
    main_pid = _main_pid(plat)
    if not boot or (main_pid and int(boot.get("pid") or 0) != main_pid):
        # The running process predates the drained restart (it never wrote a
        # boot record): the supervisor's own restart still finishes cleanly.
        click.echo("The running agent is older than the drained restart — restarting it "
                   "through the service manager.")
        _supervisor_restart(plat)
        _report_boot(data_home, t0)
        return
    req, created = lc.request_restart(
        requested_by="cli", origin={"surface": "cli"}, reason="polyrob service restart",
        drain_timeout=0.0 if no_wait else wait_idle, force=now, data_home=data_home)
    if not created:
        click.echo(f"A {req.describe()} is already open — waiting for it.")
    entry = wait_for_request(req.id, data_home=data_home,
                             timeout=(0 if no_wait else wait_idle) + 600, echo=click.echo)
    outcome = (entry or {}).get("outcome")
    if outcome == "unclaimed":
        click.echo("The running agent did not take the request — restarting it through the "
                   "service manager.")
        _supervisor_restart(plat)
        _report_boot(data_home, t0)
        return
    if entry is None:
        raise click.ClickException("no report from the service yet — check "
                                   "`polyrob service status` and `polyrob service logs`")
    detail = entry.get("detail") or outcome
    if outcome in (lc.OUTCOME_BACK_ONLINE, lc.OUTCOME_UPDATED):
        click.echo(click.style(detail, fg="green"))
        return
    raise click.ClickException(detail)


@service.command("logs")
@click.option("--follow", "-f", is_flag=True, help="Keep printing new lines.")
@click.option("--lines", "-n", type=int, default=100, show_default=True)
@_user_option
def service_logs(follow: bool, lines: int, as_user):
    """Show the background service's log."""
    if _delegated(as_user):
        return
    plat = _require_installed()
    if plat == "linux":
        from cli.commands._service_ops import user_bus_env
        cmd = ["journalctl", "--user", "-u", UNIT_NAME, "-n", str(lines), "--no-pager"]
        if follow:
            cmd.append("-f")
        raise SystemExit(subprocess.call(cmd, env=user_bus_env()))
    log = Path(_polyrob_home()) / "logs" / "service.log"
    cmd = ["tail", "-n", str(lines)] + (["-f"] if follow else []) + [str(log)]
    raise SystemExit(subprocess.call(cmd))
