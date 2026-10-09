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
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

UNIT_NAME = "polyrob.service"
LAUNCHD_LABEL = "dev.polyrob.agent"


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
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _configured_env() -> dict:
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    return dict(os.environ)


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
def service_status():
    """Is the background service installed and running?"""
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
