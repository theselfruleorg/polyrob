"""polyrob telegram — chat with the agent over Telegram via local long-polling.

No webhook / public URL / SSL needed: this long-polls getUpdates on your machine and
runs the full Task agent (same front door as the API webhook path). Owner-locked via
ALLOWED_TELEGRAM_USER_IDS (raw Telegram numeric ids); with no allowlist set the bot
replies with your id so you can lock it.

Token: --token or TELEGRAM_BOT_TOKEN (process env or ./.polyrob/.env). Nothing is committed.
"""
import asyncio
import os
from typing import Optional

import click


class TelegramTokenError(click.ClickException):
    """No Telegram bot token available."""


def resolve_telegram_token(token_opt: Optional[str]) -> str:
    """--token wins, else TELEGRAM_BOT_TOKEN env; raise a clear error if absent."""
    tok = (token_opt or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not tok:
        raise TelegramTokenError(
            "No Telegram bot token. Pass --token, or set TELEGRAM_BOT_TOKEN "
            "(process env or ./.polyrob/.env). Create a bot via @BotFather to get one."
        )
    return tok


@click.command()
@click.option("--token", default=None, help="Telegram bot token (else TELEGRAM_BOT_TOKEN env)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def telegram(token: Optional[str], verbose: bool):
    """Run the agent as a Telegram bot (local long-polling)."""
    asyncio.run(_run_telegram(token, verbose))


async def _run_telegram(token_opt: Optional[str], verbose: bool):
    from cli.commands._surface_runner import SurfaceJob, run_surface

    async def _build(ctx):
        from surfaces.telegram.harness import build_telegram_harness
        harness = build_telegram_harness(ctx.container, ctx.task_agent,
                                         token=ctx.creds, webhook_base=None,
                                         data_dir=ctx.data_dir)

        # start() registers + subscribes the surface and clears any stale webhook.
        # Runs BEFORE the autonomy loops start — crucially the shared runtime is what
        # runs the #6 surface-GC ticker that prunes stale chat<->session bindings;
        # without it GC would be dead on the primary Telegram surface (the one that
        # mints those bindings).
        await harness.start()

        async def _announce():
            # Greet: confirm which bot we're driving + the owner-lock state.
            try:
                me = await harness.bot.get_me()
                username = getattr(me, "username", None)
            except Exception:
                username = None
            # W3 groups (2026-07-14): the inbound builder needs our own username to
            # detect @mentions / replies-to-us — without it every group message is
            # silently denied.
            harness.bot_username = username
            allow = (os.environ.get("ALLOWED_TELEGRAM_USER_IDS") or "").strip()
            click.echo(click.style("telegram bot online", fg="green")
                       + (f": @{username}" if username else ""))
            if allow:
                click.echo(click.style(f"owner-locked to user id(s): {allow}", dim=True))
            else:
                click.echo(click.style(
                    "no allowlist set — message the bot once and it will reply with your id, "
                    "then set ALLOWED_TELEGRAM_USER_IDS and restart.", fg="yellow"))
            click.echo(click.style("polling… (Ctrl-C to stop)", dim=True))

        def _halt():
            harness._running = False

        return SurfaceJob(harness=harness, run=harness.run_polling,
                          announce=_announce, on_stop=_halt)

    await run_surface(
        extra_env={"TELEGRAM_SURFACE_ENABLED": "true"},
        verbose=verbose,
        resolve_credentials=lambda: resolve_telegram_token(token_opt),
        silence_loggers=("httpx", "httpcore", "aiogram.event", "asyncio", "hpack"),
        build_harness=_build,
        stopping_message="stopping telegram bot…",
    )
