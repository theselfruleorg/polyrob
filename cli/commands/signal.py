"""polyrob signal — chat with the agent over Signal via a signal-cli daemon (W4).

Requires a running signal-cli HTTP daemon with a linked account:
    signal-cli -a +<E164> daemon --http=127.0.0.1:8080
Config: --daemon-url/SIGNAL_DAEMON_URL, --account/SIGNAL_ACCOUNT.
"""
import asyncio
import os
import signal as _signal
import sys
from typing import Optional

import click
from core.runtime_paths import data_dir_or_home


class SignalConfigError(click.ClickException):
    """Missing Signal daemon config."""


def resolve_signal_config(daemon_opt: Optional[str],
                          account_opt: Optional[str]) -> tuple[str, str]:
    daemon = (daemon_opt or os.environ.get("SIGNAL_DAEMON_URL")
              or "http://127.0.0.1:8080").strip()
    account = (account_opt or os.environ.get("SIGNAL_ACCOUNT") or "").strip()
    if not account:
        raise SignalConfigError(
            "No Signal account. Pass --account or set SIGNAL_ACCOUNT "
            "(the +E164 number linked in signal-cli)."
        )
    return daemon, account


@click.command()
@click.option("--daemon-url", default=None,
              help="signal-cli HTTP daemon URL (else SIGNAL_DAEMON_URL, "
                   "default http://127.0.0.1:8080)")
@click.option("--account", default=None,
              help="Signal account +E164 (else SIGNAL_ACCOUNT env)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def signal(daemon_url: Optional[str], account: Optional[str], verbose: bool):
    """Run the agent over Signal (signal-cli daemon)."""
    asyncio.run(_run_signal(daemon_url, account, verbose))


async def _run_signal(daemon_opt, account_opt, verbose: bool):
    import logging as _logging

    from core.bootstrap import (build_cli_container, setup_project_path,
                                setup_sqlite_compat)

    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("SIGNAL_SURFACE_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    daemon_url, account = resolve_signal_config(daemon_opt, account_opt)

    headless = not sys.stderr.isatty()
    log_level = "DEBUG" if verbose else ("INFO" if headless else "ERROR")
    quiet = (not verbose) and (not headless)
    if quiet:
        _logging.disable(_logging.CRITICAL)
    try:
        container = await build_cli_container(log_level=log_level)
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + f"failed to start: {e}")
        sys.exit(1)
    if quiet:
        _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "TaskAgent not available in container")
        sys.exit(1)

    from core.surfaces.bootstrap import install_surface_bus
    install_surface_bus(container)
    dispatcher = container.get_service("outbound_dispatcher")
    if dispatcher is not None:
        dispatcher.start()

    from surfaces.signal.harness import build_signal_harness
    _data_dir = data_dir_or_home(
        getattr(getattr(container, "config", None), "data_dir", None))
    harness = build_signal_harness(container, task_agent,
                                   daemon_url=daemon_url, account=account,
                                   data_dir=_data_dir)

    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            autonomy_handles = start_autonomy(task_agent=task_agent,
                                              data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    click.echo(click.style("signal bot online", fg="green")
               + f" — account {account} via {daemon_url}")
    click.echo(click.style("listening… (Ctrl-C to stop)", dim=True))

    loop = asyncio.get_running_loop()
    run_task = asyncio.ensure_future(harness.run())

    def _stop(*_a):
        run_task.cancel()

    try:
        loop.add_signal_handler(_signal.SIGINT, _stop)
        loop.add_signal_handler(_signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        await run_task
    except asyncio.CancelledError:
        pass
    finally:
        click.echo("\n" + click.style("stopping signal bot…", dim=True))
        if autonomy_handles is not None:
            try:
                await autonomy_handles.stop()
            except Exception:
                pass
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except Exception:
                pass
        await harness.stop()
