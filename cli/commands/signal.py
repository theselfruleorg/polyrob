"""polyrob signal — chat with the agent over Signal via a signal-cli daemon (W4).

Requires a running signal-cli HTTP daemon with a linked account:
    signal-cli -a +<E164> daemon --http=127.0.0.1:8080
Config: --daemon-url/SIGNAL_DAEMON_URL, --account/SIGNAL_ACCOUNT.
"""
import asyncio
import os
from typing import Optional

import click


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
    from cli.commands._surface_runner import SurfaceJob, run_surface

    async def _build(ctx):
        daemon_url, account = ctx.creds
        from surfaces.signal.harness import build_signal_harness
        harness = build_signal_harness(ctx.container, ctx.task_agent,
                                       daemon_url=daemon_url, account=account,
                                       data_dir=ctx.data_dir)

        async def _announce():
            click.echo(click.style("signal bot online", fg="green")
                       + f" — account {account} via {daemon_url}")
            click.echo(click.style("listening… (Ctrl-C to stop)", dim=True))

        return SurfaceJob(harness=harness, run=harness.run, announce=_announce)

    await run_surface(
        extra_env={"SIGNAL_SURFACE_ENABLED": "true"},
        verbose=verbose,
        resolve_credentials=lambda: resolve_signal_config(daemon_opt, account_opt),
        build_harness=_build,
        stopping_message="stopping signal bot…",
    )
