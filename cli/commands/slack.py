"""polyrob slack — chat with the agent over Slack (Socket Mode, W4).

No public URL needed: Socket Mode connects out to Slack. Needs TWO tokens:
a bot token (``xoxb-``, chat scopes) and an app-level token (``xapp-``,
``connections:write``). Enable Socket Mode + the Events API message events
in your Slack app config.
"""
import asyncio
import os
import signal as _signal
import sys
from typing import Optional

import click
from core.runtime_paths import data_dir_or_home


class SlackTokenError(click.ClickException):
    """Missing Slack token(s)."""


def resolve_slack_tokens(bot_opt: Optional[str],
                         app_opt: Optional[str]) -> tuple[str, str]:
    bot = (bot_opt or os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    app = (app_opt or os.environ.get("SLACK_APP_TOKEN") or "").strip()
    if not bot or not app:
        raise SlackTokenError(
            "Slack needs BOTH tokens: --bot-token/SLACK_BOT_TOKEN (xoxb-) and "
            "--app-token/SLACK_APP_TOKEN (xapp-, Socket Mode). Configure them "
            "at https://api.slack.com/apps."
        )
    return bot, app


@click.command()
@click.option("--bot-token", default=None,
              help="Slack bot token xoxb- (else SLACK_BOT_TOKEN env)")
@click.option("--app-token", default=None,
              help="Slack app-level token xapp- (else SLACK_APP_TOKEN env)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def slack(bot_token: Optional[str], app_token: Optional[str], verbose: bool):
    """Run the agent as a Slack bot (Socket Mode)."""
    asyncio.run(_run_slack(bot_token, app_token, verbose))


async def _run_slack(bot_opt, app_opt, verbose: bool):
    import logging as _logging

    from core.bootstrap import (build_cli_container, setup_project_path,
                                setup_sqlite_compat)

    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("SLACK_SURFACE_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    bot_token, app_token = resolve_slack_tokens(bot_opt, app_opt)

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

    from surfaces.slack.harness import build_slack_harness
    _data_dir = data_dir_or_home(
        getattr(getattr(container, "config", None), "data_dir", None))
    harness = build_slack_harness(container, task_agent, bot_token=bot_token,
                                  app_token=app_token, data_dir=_data_dir)

    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            autonomy_handles = start_autonomy(task_agent=task_agent,
                                              data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    click.echo(click.style("slack bot online", fg="green")
               + " — connecting via Socket Mode…")
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
        click.echo("\n" + click.style("stopping slack bot…", dim=True))
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
