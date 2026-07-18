"""polyrob discord — chat with the agent over Discord (Gateway WS, W3 T4/T5).

No public URL needed: connects to the Discord Gateway as a bot, receives DMs
and (with GROUP_CHAT_ENABLED + an allowlisted channel) guild messages, and
runs the full Task agent. Group messages are mention-gated by default.

Token: --token or DISCORD_BOT_TOKEN (process env or ./.polyrob/.env). Create a
bot at https://discord.com/developers/applications, enable the MESSAGE CONTENT
intent, and invite it with the bot scope.
"""
import asyncio
import os
import signal
import sys
from typing import Optional

import click
from core.runtime_paths import data_dir_or_home


class DiscordTokenError(click.ClickException):
    """No Discord bot token available."""


def resolve_discord_token(token_opt: Optional[str]) -> str:
    """--token wins, else DISCORD_BOT_TOKEN env; raise a clear error if absent."""
    tok = (token_opt or os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not tok:
        raise DiscordTokenError(
            "No Discord bot token. Pass --token, or set DISCORD_BOT_TOKEN "
            "(process env or ./.polyrob/.env). Create a bot at "
            "https://discord.com/developers/applications."
        )
    return tok


@click.command()
@click.option("--token", default=None,
              help="Discord bot token (else DISCORD_BOT_TOKEN env)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def discord(token: Optional[str], verbose: bool):
    """Run the agent as a Discord bot (Gateway websocket)."""
    asyncio.run(_run_discord(token, verbose))


async def _run_discord(token_opt: Optional[str], verbose: bool):
    import logging as _logging

    from core.bootstrap import (build_cli_container, setup_project_path,
                                setup_sqlite_compat)

    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("DISCORD_SURFACE_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    token = resolve_discord_token(token_opt)

    headless = not sys.stderr.isatty()
    if verbose:
        log_level = "DEBUG"
    elif headless:
        log_level = "INFO"
    else:
        log_level = "ERROR"
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
    elif not verbose:
        for _noisy in ("httpx", "httpcore", "aiohttp", "asyncio"):
            _logging.getLogger(_noisy).setLevel(_logging.WARNING)

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

    from surfaces.discord.harness import build_discord_harness
    _data_dir = data_dir_or_home(
        getattr(getattr(container, "config", None), "data_dir", None))
    harness = build_discord_harness(container, task_agent, token=token,
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

    click.echo(click.style("discord bot online", fg="green")
               + " — connecting to gateway…")
    if os.environ.get("GROUP_CHAT_ENABLED", "").strip().lower() in (
            "1", "true", "yes", "on"):
        click.echo(click.style(
            "group chat ON — only allowlisted channels respond "
            "(polyrob owner groups allow discord <channel_id>)", dim=True))
    click.echo(click.style("listening… (Ctrl-C to stop)", dim=True))

    loop = asyncio.get_running_loop()
    run_task = asyncio.ensure_future(harness.run())

    def _stop(*_a):
        run_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError):
        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(_sig, lambda *_a: _stop())
            except (ValueError, OSError, AttributeError):
                pass

    try:
        await run_task
    except asyncio.CancelledError:
        pass
    finally:
        click.echo("\n" + click.style("stopping discord bot…", dim=True))
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
