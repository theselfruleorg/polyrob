"""``polyrob apps`` — the durable app service (proposal 032): the owner seat
(list/show/approve/reject/kill/logs) and the supervisor loop the unit runs.

Lazy-registered (cli/polyrob.py::_LAZY_SUBCOMMANDS); nothing heavy at import.
"""
from __future__ import annotations

import asyncio
import json as _json
import os

import click


@click.group("apps")
def apps():
    """Durable apps: approve an address, watch health, kill, read logs, supervise."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _data_dir() -> str:
    from cli._admin_home import admin_data_dir
    return admin_data_dir()


def _registry():
    from core.app_service.registry import AppServiceRegistry, default_app_services_db
    return AppServiceRegistry(default_app_services_db(data_dir=_data_dir()))


def _tenant(user):
    """The ONE owner-tenant resolver, so `polyrob apps …`, the console's `/apps`
    and the agent's own rows name one bucket."""
    from core.instance import resolve_owner_user_id
    return user or resolve_owner_user_id()


async def default_sys_runner(argv, *, input=None, timeout=None):
    """nginx / systemctl / nft through the same process-group runner docker uses."""
    from tools.code_exec.backends._proc import run_group
    code, out, err, _timed_out = await run_group(
        list(argv), stdin_bytes=input.encode() if input else None, timeout=timeout,
        label=str(argv[0]))
    return code, out, err


def build_supervisor():
    """The production supervisor from the flag readers (SSOT docs/CONFIGURATION.md)."""
    from core.app_service import config as c
    from core.app_service.egress import EgressApplier
    from core.app_service.nginx import NginxApplier
    from core.app_service.supervisor import AppSupervisor
    from tools.code_exec.backends.docker import _default_docker_runner
    data_dir = _data_dir()
    roots = [data_dir, os.getcwd()]
    for extra in (os.getenv("POLYROB_PROJECT_DIR"), os.getenv("APP_SERVICE_SOURCE_ROOT")):
        if extra:
            roots.append(extra)
    return AppSupervisor(
        _registry(), data_dir=data_dir, base_domain=c.app_service_base_domain(),
        allow_public=c.app_service_allow_public(), docker_runner=_default_docker_runner,
        sys_runner=default_sys_runner,
        nginx=NginxApplier(c.app_service_nginx_conf_dir(), default_sys_runner),
        egress=EgressApplier(default_sys_runner), image=c.app_service_image(),
        memory_mb=c.app_service_memory_mb(), cpus=c.app_service_cpus(), pids=c.app_service_pids(),
        snapshot_max_mb=c.app_service_snapshot_max_mb(), port_range=c.app_service_port_range(),
        health_timeout_sec=c.app_service_health_timeout_sec(), cert_dir=c.app_service_cert_dir(),
        source_roots=roots, retain_days=c.app_service_retain_days(),
    )


@apps.command("supervise")
@click.option("--once", is_flag=True, help="One reconcile tick, then exit.")
@click.option("--interval", type=int, default=None, help="Seconds between ticks (APP_SERVICE_TICK_SEC).")
def supervise(once, interval):
    """Run the reconcile loop (what polyrob-apps.service executes)."""
    from core.app_service.config import app_service_enabled, app_service_tick_sec
    from core.app_service.supervisor import run_forever
    if not app_service_enabled():
        raise click.ClickException("APP_SERVICE_ENABLED is off (or AGENT_BUILDER_MODE is not 'ship') "
                                   "— nothing to supervise")
    sup = build_supervisor()
    every = interval or app_service_tick_sec()

    async def _main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            import signal
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        await run_forever(sup, every, once=once, stop_event=stop)

    asyncio.run(_main())


# --- owner verbs (the SAME helpers Telegram / REPL / the console render from) ----

def _print_lines(lines):
    for ln in lines:
        click.echo(ln)


@apps.command("list")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def list_cmd(user, as_json):
    """List this tenant's apps with status, URL and last health."""
    from core.app_service.owner_ops import list_lines, row_json
    reg = _registry()
    tenant = _tenant(user)
    if as_json:
        click.echo(_json.dumps([row_json(r) for r in reg.list_for(tenant)], indent=2, default=str))
        return
    _print_lines(list_lines(reg, tenant))


@apps.command("show")
@click.argument("slug")
@click.option("--user", default=None)
def show_cmd(slug, user):
    """Show one app in full."""
    from core.app_service.owner_ops import show_lines
    _print_lines(show_lines(_registry(), _tenant(user), slug))


@apps.command("approve")
@click.argument("slug")
@click.option("--user", default=None)
def approve_cmd(slug, user):
    """Approve a PENDING app's address (the owner decision the agent waits for)."""
    from core.app_service.owner_ops import approve
    ok, msg = approve(_registry(), slug, _tenant(user), via="cli")
    click.echo(msg)
    if not ok:
        raise SystemExit(1)


@apps.command("reject")
@click.argument("slug")
@click.option("--user", default=None)
def reject_cmd(slug, user):
    """Reject a PENDING app (it never runs)."""
    from core.app_service.owner_ops import reject
    ok, msg = reject(_registry(), slug, _tenant(user), via="cli")
    click.echo(msg)
    if not ok:
        raise SystemExit(1)


@apps.command("kill")
@click.argument("slug")
@click.option("--user", default=None)
def kill_cmd(slug, user):
    """Stop a running app (container + stanza removed on the next tick)."""
    from core.app_service.owner_ops import kill
    ok, msg = kill(_registry(), slug, _tenant(user), via="cli")
    click.echo(msg)
    if not ok:
        raise SystemExit(1)


@apps.command("logs")
@click.argument("slug")
@click.option("-n", "lines", type=int, default=50, help="Trailing lines (1-400).")
@click.option("--user", default=None)
def logs_cmd(slug, lines, user):
    """Read an app's recent log lines (refreshed by the supervisor each tick)."""
    from core.app_service.owner_ops import logs_tail
    click.echo(logs_tail(_data_dir(), _tenant(user), slug, lines), nl=False)
    click.echo()
