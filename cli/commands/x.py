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
import signal
import sys
from typing import Optional

import click
from core.runtime_paths import data_dir_or_home


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
    import logging as _logging

    from core.bootstrap import (build_cli_container, setup_project_path,
                                setup_sqlite_compat)

    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("X_SURFACE_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    check_x_credentials()

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

    from surfaces.x.harness import build_x_harness
    _data_dir = data_dir_or_home(
        getattr(getattr(container, "config", None), "data_dir", None))
    harness = build_x_harness(container, task_agent, data_dir=_data_dir)

    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            autonomy_handles = start_autonomy(task_agent=task_agent,
                                              data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    click.echo(click.style("x dm bot online", fg="green")
               + f" — polling dm_events every {os.getenv('X_DM_POLL_SEC', '90')}s…")
    click.echo(click.style(
        "note: X allows 15 DM reads + 15 DM sends per 15 min — "
        "replies can lag under load", dim=True))
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
        click.echo("\n" + click.style("stopping x dm bot…", dim=True))
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
