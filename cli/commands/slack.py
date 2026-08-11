"""polyrob slack — chat with the agent over Slack (Socket Mode, W4).

No public URL needed: Socket Mode connects out to Slack. Needs TWO tokens:
a bot token (``xoxb-``, chat scopes) and an app-level token (``xapp-``,
``connections:write``). Enable Socket Mode + the Events API message events
in your Slack app config.
"""
import asyncio
import os
from typing import Optional

import click


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
    from cli.commands._surface_runner import SurfaceJob, run_surface

    async def _build(ctx):
        bot_token, app_token = ctx.creds
        from surfaces.slack.harness import build_slack_harness
        harness = build_slack_harness(ctx.container, ctx.task_agent,
                                      bot_token=bot_token, app_token=app_token,
                                      data_dir=ctx.data_dir)

        async def _announce():
            click.echo(click.style("slack bot online", fg="green")
                       + " — connecting via Socket Mode…")
            click.echo(click.style("listening… (Ctrl-C to stop)", dim=True))

        return SurfaceJob(harness=harness, run=harness.run, announce=_announce)

    await run_surface(
        extra_env={"SLACK_SURFACE_ENABLED": "true"},
        verbose=verbose,
        resolve_credentials=lambda: resolve_slack_tokens(bot_opt, app_opt),
        build_harness=_build,
        stopping_message="stopping slack bot…",
    )
