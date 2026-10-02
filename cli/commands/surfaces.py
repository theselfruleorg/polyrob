"""polyrob surfaces — every chat surface from the surface catalog (064 F6).

    polyrob surfaces list [--probe]   Each surface: enabled / configured / probe state.
    polyrob surfaces add <id>         Ask for exactly that surface's credentials and
                                      write them to the env file (never echoed).
    polyrob surfaces probe <id|all>   Prove a credential with a READ call — never a send.

Everything comes from ``core/surfaces/catalog.py``: a new surface row appears here
with no edit. The probe for a surface is its package's ``probe.py``
(``surfaces/_probe.py``). A credential that is absent, or a platform that cannot
be reached, is ``unavailable(<reason>)`` — never a silent pass.

(Distinct from ``polyrob surface`` — singular — the per-surface circuit breaker.)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import click


#: Set when the deployment's env file exists but this user may not read it.
_DEPLOYED_UNREADABLE = "__deployed_env_unreadable__"


def _surface_keys() -> set:
    from core.surfaces.catalog import surfaces as catalog
    keys = {"OPENAI_API_KEY", "EMAIL_PROVIDER", "GMAIL_IMAP_SERVER", "AUTONOMY_MODE"}
    for spec in catalog():
        keys.update(spec.credentials)
        keys.add(spec.enabled_flag)
        for alt in spec.alt_required:
            keys.update(alt)
    return keys


def _load_env() -> dict:
    """The env the surfaces would see: this shell's layers, then — for every key
    the shell leaves unset — the DEPLOYED instance's env file (the 031 deployed-
    home rule: an owner verb on the prod box describes the running instance, not
    an empty shell). An env file this user may not read is recorded as such, so
    a credential reads "cannot tell", never "missing"."""
    from core.bootstrap import load_env
    try:
        load_env(local_mode=True)
    except Exception:
        pass
    env = dict(os.environ)
    from core.admin_data_home import (DEPLOYED_ENV_FILE, DeployedEnvUnreadable,
                                      deployed_env_value)
    if not os.path.exists(DEPLOYED_ENV_FILE):
        return env
    for key in sorted(_surface_keys()):
        if (env.get(key) or "").strip():
            continue
        try:
            value = deployed_env_value(key)
        except DeployedEnvUnreadable:
            env[_DEPLOYED_UNREADABLE] = DEPLOYED_ENV_FILE
            break
        if value:
            env[key] = value
    return env


def _running(spec) -> str:
    """Which live process carries this surface: its standalone command, or the
    gateway with its flag on. ``""`` when none; ``"unknown"`` where the process
    table cannot be read."""
    try:
        from cli.update.process_guard import _iter_cmdlines
        found = []
        for pid, argv in _iter_cmdlines():
            if not any(os.path.basename(a) in ("polyrob", "polyrob.py") or
                       a.endswith("cli/polyrob.py") for a in argv[:3]):
                continue
            rest = [a for a in argv[1:] if not a.startswith("-")]
            if spec.id in rest:
                found.append(f"polyrob {spec.id} (pid {pid})")
            elif "gateway" in rest:
                found.append(f"polyrob gateway (pid {pid})")
        return ", ".join(found)
    except Exception:
        return "unknown"


def _load_packs() -> None:
    """067 P3b: a pack's surface row is listed and probed only while its pack
    loads here; otherwise it is named with the reason (``withheld_reason``)."""
    try:
        from core.packs.loader import load_packs
        load_packs()
    except Exception:  # noqa: BLE001 — never stops the surfaces verb
        pass


def _spec_or_fail(surface_id: str):
    from core.surfaces.catalog import get, surface_ids, withheld_reason
    _load_packs()
    spec = get(surface_id)
    if spec is None:
        why = withheld_reason(surface_id)
        if why:
            raise click.ClickException(f"surface {surface_id!r} is not available: {why}")
        raise click.ClickException(
            f"unknown surface {surface_id!r} (known: {', '.join(surface_ids())})")
    return spec


def _configured(spec, env: dict) -> str:
    """``yes`` | ``missing A, B`` | ``cannot tell (…)`` — from the catalog row."""
    if not spec.credentials:
        return "no credentials listed"
    absent = spec.missing_credentials(env)
    if not absent:
        return "yes"
    if env.get(_DEPLOYED_UNREADABLE):
        return (f"cannot tell — {env[_DEPLOYED_UNREADABLE]} is not readable by this "
                f"user (run with sudo)")
    return "missing " + ", ".join(absent)


async def _probe(spec, env: dict):
    import importlib

    from surfaces._probe import ProbeResult
    try:
        mod = importlib.import_module(f"{spec.module}.probe")
    except ModuleNotFoundError as e:
        if e.name != f"{spec.module}.probe":
            return ProbeResult.unavailable(f"missing dependency {e.name}")
        return ProbeResult.unavailable("no probe for this surface")
    try:
        return await mod.probe(env)
    except Exception as e:  # a broken probe is named, never a pass
        return ProbeResult.unavailable(f"probe raised {type(e).__name__}")


@click.group()
def surfaces():
    """List, add and probe chat surfaces (Telegram, Slack, Discord, …)."""


@surfaces.command("list")
@click.option("--probe", "do_probe", is_flag=True,
              help="Also prove each configured credential with a read call.")
def list_cmd(do_probe: bool):
    """Every surface: enabled flag, credentials, probe state."""
    from core.surfaces.catalog import surfaces as catalog, withheld_surfaces
    from core.surfaces.config import SurfaceConfig
    _load_packs()
    env = _load_env()
    rows = []
    for spec in catalog():
        flag = (env.get(spec.enabled_flag) or "").strip()
        if flag:
            from core.env import parse_bool
            enabled = parse_bool(flag, False)
        else:
            enabled = SurfaceConfig.surface_enabled(spec.id)   # the gateway's own default
        configured = _configured(spec, env)
        if do_probe and configured.startswith("yes"):
            state = asyncio.run(_probe(spec, env)).render()
        elif do_probe:
            state = "unavailable(not configured)"
        else:
            state = "not probed"
        rows.append((spec, enabled, configured, state, _running(spec)))
    width = max(len(s.id) for s, *_ in rows)
    for spec, enabled, configured, state, running in rows:
        flag = click.style("on ", fg="green") if enabled else click.style("off", dim=True)
        click.echo(f"{spec.id.ljust(width)}  {flag}  {spec.label} · {spec.transport}"
                   f"{' · extra ' + spec.extra if spec.extra else ''}")
        click.echo(f"{' ' * width}       credentials: {configured} · probe: {state}")
        if running:
            click.echo(f"{' ' * width}       running: {running}")
    for spec, why in withheld_surfaces():
        click.echo(f"{spec.id.ljust(width)}  " + click.style("n/a", fg="yellow")
                   + f"  {spec.label} · {why}")
    click.echo(click.style("on/off = the gateway flag; a standalone `polyrob <surface>` "
                           "runs regardless of it.", dim=True))


@surfaces.command("probe")
@click.argument("surface_id")
def probe_cmd(surface_id: str):
    """Prove SURFACE_ID's credential with a read (``all`` = every configured one)."""
    from core.surfaces.catalog import surfaces as catalog
    _load_packs()
    env = _load_env()
    specs = list(catalog()) if surface_id == "all" else [_spec_or_fail(surface_id)]
    worst = 0
    if env.get(_DEPLOYED_UNREADABLE):
        click.echo(click.style(f"note: {env[_DEPLOYED_UNREADABLE]} is not readable by "
                               f"this user — run with sudo to probe the deployed "
                               f"credentials.", fg="yellow"))
    for spec in specs:
        result = asyncio.run(_probe(spec, env))
        colour = {"ok": "green", "failed": "red"}.get(result.state, "yellow")
        click.echo(f"{spec.id}: " + click.style(result.render(), fg=colour))
        if result.state == "failed":
            worst = max(worst, 2)
        elif result.state == "unavailable" and surface_id != "all":
            worst = max(worst, 1)
    if worst:
        raise SystemExit(worst)


def _env_target(env_file: Optional[str]) -> Path:
    """``--env-file``, else the DEPLOYED env file on a deployed box (that is what
    the running service reads), else ``~/.polyrob/.env``."""
    if env_file:
        return Path(env_file).expanduser()
    from core.admin_data_home import DEPLOYED_ENV_FILE
    if os.path.exists(DEPLOYED_ENV_FILE):
        if not os.access(DEPLOYED_ENV_FILE, os.W_OK):
            raise click.ClickException(
                f"{DEPLOYED_ENV_FILE} is the running service's env file and this user "
                f"cannot write it — run with sudo, or pass --env-file.")
        return Path(DEPLOYED_ENV_FILE)
    from core.paths import polyrob_home
    return polyrob_home() / ".env"


@surfaces.command("add")
@click.argument("surface_id")
@click.option("--env-file", default=None, metavar="PATH",
              help="Env file to write (default: /etc/polyrob/polyrob.env on a deployed "
                   "box, else ~/.polyrob/.env — the active profile's).")
@click.option("--enable/--no-enable", default=True, show_default=True,
              help="Also set the surface's *_SURFACE_ENABLED=true.")
@click.option("--no-probe", is_flag=True, help="Skip the read probe after writing.")
def add_cmd(surface_id: str, env_file: Optional[str], enable: bool, no_probe: bool):
    """Ask for SURFACE_ID's credentials and write them (values never echoed)."""
    from core.env_file import env_write_error, upsert_env_var
    from core.security.redaction import fingerprint
    spec = _spec_or_fail(surface_id)
    target = _env_target(env_file)
    env = _load_env()
    written = []
    click.echo(f"{spec.label}: {len(spec.credentials)} credential(s). "
               f"Press Enter to keep a value that is already set.")
    for key in spec.credentials:
        have = (env.get(key) or "").strip()
        hint = f" [set: {fingerprint(have)}]" if have else ""
        value = click.prompt(f"  {key}{hint}", default="", show_default=False,
                             hide_input=True).strip()
        if not value:
            continue
        err = env_write_error(key, value)
        if err:
            raise click.ClickException(err)
        upsert_env_var(target, key, value, secure=True)
        env[key] = value
        written.append(key)
        click.echo(f"    wrote {key} = {fingerprint(value)}")
    if enable:
        upsert_env_var(target, spec.enabled_flag, "true", secure=True)
        env[spec.enabled_flag] = "true"
        written.append(spec.enabled_flag)
    click.echo(f"{len(written)} line(s) written to {target}")
    if spec.extra:
        # 064 factory audit: a new surface's SDK is an opt-in extra, and neither
        # `polyrob update` nor the installer adds a NEW extra on its own.
        from core.optional_extras import extra_available, pip_hint
        if not extra_available(spec.extra):
            click.echo(click.style(f"{spec.label} needs its SDK: {pip_hint(spec.extra)}",
                                   fg="yellow"))
    if spec.transport == "webhook":
        click.echo(f"Register this webhook URL with the platform: "
                   f"https://<your-host>/webhooks/{spec.id}")
    if not no_probe:
        result = asyncio.run(_probe(spec, env))
        click.echo(f"probe: {result.render()}")
    click.echo("Restart the process that runs this surface (the gateway, "
               "`polyrob service restart`, or its systemd unit) to pick it up.")
