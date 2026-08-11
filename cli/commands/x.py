"""polyrob x — chat with the agent over X (Twitter) DMs.

No public URL needed: polls ``GET /2/dm_events`` with the account's OAuth 1.0a
user-context creds (the SAME ``TWITTER_*`` env vars the twitter tool uses) and
replies via ``POST /2/dm_conversations/with/:participant_id/messages``.

Rate-limit reality (pay-per-use tier, docs.x.com 2026-07): DM reads are
15 req/15 min per user, so the poll interval defaults to 90s (``X_DM_POLL_SEC``)
— faster polling just burns the window and gets 429s. Sends are 15/15 min +
1,440/24 h. Group DM conversations are not handled in v1.
"""
import asyncio
import os

import click


class XCredentialsError(click.ClickException):
    """Missing X (Twitter) OAuth1 user-context credentials."""


_REQUIRED_ENVS = ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
                  "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")


def check_x_credentials() -> None:
    """DM endpoints need OAuth1 USER context — all four creds, not the bearer."""
    missing = [k for k in _REQUIRED_ENVS if not (os.environ.get(k) or "").strip()]
    if missing:
        raise XCredentialsError(
            "Missing X (Twitter) credentials: " + ", ".join(missing) + ". "
            "DMs need OAuth 1.0a user-context keys (bearer-only won't work) — "
            "set them in the env or ./.polyrob/.env."
        )


@click.command(name="x")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def x(verbose: bool):
    """Run the agent as an X (Twitter) DM bot (polling)."""
    asyncio.run(_run_x(verbose))


async def _run_x(verbose: bool):
    from cli.commands._surface_runner import SurfaceJob, run_surface

    async def _build(ctx):
        from surfaces.x.harness import build_x_harness
        harness = build_x_harness(ctx.container, ctx.task_agent,
                                  data_dir=ctx.data_dir)

        async def _announce():
            click.echo(click.style("x dm bot online", fg="green")
                       + f" — polling dm_events every {os.getenv('X_DM_POLL_SEC', '90')}s…")
            click.echo(click.style(
                "note: X allows 15 DM reads + 15 DM sends per 15 min — "
                "replies can lag under load", dim=True))
            click.echo(click.style("listening… (Ctrl-C to stop)", dim=True))

        return SurfaceJob(harness=harness, run=harness.run, announce=_announce)

    await run_surface(
        extra_env={"X_SURFACE_ENABLED": "true"},
        verbose=verbose,
        resolve_credentials=check_x_credentials,
        silence_loggers=("httpx", "httpcore", "aiohttp", "asyncio"),
        build_harness=_build,
        stopping_message="stopping x dm bot…",
    )
