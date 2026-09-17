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
from typing import Optional

import click


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
    from cli.commands._surface_runner import SurfaceJob, run_surface

    async def _build(ctx):
        from surfaces.discord.harness import build_discord_harness
        harness = build_discord_harness(ctx.container, ctx.task_agent,
                                        token=ctx.creds, data_dir=ctx.data_dir)

        async def _announce():
            click.echo(click.style("discord bot online", fg="green")
                       + " — connecting to gateway…")
            from core.env import bool_env
            if bool_env("GROUP_CHAT_ENABLED", False):
                click.echo(click.style(
                    "group chat ON — only allowlisted channels respond "
                    "(polyrob owner groups allow discord <channel_id>)", dim=True))
            click.echo(click.style("listening… (Ctrl-C to stop)", dim=True))

        return SurfaceJob(harness=harness, run=harness.run, announce=_announce)

    await run_surface(
        extra_env={"DISCORD_SURFACE_ENABLED": "true"},
        verbose=verbose,
        resolve_credentials=lambda: resolve_discord_token(token_opt),
        silence_loggers=("httpx", "httpcore", "aiohttp", "asyncio"),
        build_harness=_build,
        stopping_message="stopping discord bot…",
    )
