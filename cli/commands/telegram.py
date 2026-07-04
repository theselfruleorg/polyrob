"""polyrob telegram — chat with the agent over Telegram via local long-polling.

No webhook / public URL / SSL needed: this long-polls getUpdates on your machine and
runs the full Task agent (same front door as the API webhook path). Owner-locked via
ALLOWED_TELEGRAM_USER_IDS (raw Telegram numeric ids); with no allowlist set the bot
replies with your id so you can lock it.

Token: --token or TELEGRAM_BOT_TOKEN (process env or ./.polyrob/.env). Nothing is committed.
"""
import asyncio
import os
import signal
import sys
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
    import logging as _logging

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    # The outbound bus + binding are gated on SINGULAR_CHAT_ENABLED; the surface gate
    # is TELEGRAM_SURFACE_ENABLED. Default them ON for this command (explicit env wins),
    # BEFORE the container build so the bus installs during TaskAgent construction too.
    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("TELEGRAM_SURFACE_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    # No usable provider key → clean canonical message + exit (a daemon must not block
    # on an interactive wizard). Loads env layers internally.
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    # Resolve the bot token NOW — preflight_or_onboard already loaded the env layers
    # (./.polyrob/.env etc.), so fail fast on a missing token (a clean ClickException)
    # instead of paying a full container build first.
    token = resolve_telegram_token(token_opt)

    # A headless run (systemd: stderr is the journal, not a TTY) MUST log — a server
    # with no logs is unoperable. An interactive run keeps the console clean. `-v` always
    # gives DEBUG. So: verbose->DEBUG; headless->INFO to the journal; interactive->quiet.
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
        os.environ["GRPC_VERBOSITY"] = "ERROR"
        os.environ["GLOG_minloglevel"] = "3"

    try:
        container = await build_cli_container(log_level=log_level)
    except Exception as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to start: {e}")
        sys.exit(1)
    if quiet:
        _logging.disable(_logging.ERROR)
    elif not verbose:
        # Headless service: app logs at INFO in the journal, but silence the noisy
        # transport libraries (long-poll getUpdates, aiogram dispatcher, asyncio).
        for _noisy in ("httpx", "httpcore", "aiogram.event", "asyncio", "hpack"):
            _logging.getLogger(_noisy).setLevel(_logging.WARNING)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + "TaskAgent not available in container")
        sys.exit(1)

    # Install the outbound bus (idempotent; gated on SINGULAR_CHAT_ENABLED) so agent
    # replies can route out to the Telegram surface.
    from core.surfaces.bootstrap import install_surface_bus
    install_surface_bus(container)  # db_path defaults to container.config.data_dir

    dispatcher = container.get_service("outbound_dispatcher")
    if dispatcher is not None:
        dispatcher.start()

    from surfaces.telegram.harness import build_telegram_harness
    # Pin harness state DBs (dedup / user directory) to the container's data_dir so
    # per-instance isolation holds under POLYROB_DATA_DIR (else they land in ./data).
    _data_dir = getattr(getattr(container, "config", None), "data_dir", "data") or "data"
    harness = build_telegram_harness(container, task_agent, token=token, webhook_base=None,
                                     data_dir=_data_dir)

    # start() registers + subscribes the surface and clears any stale webhook.
    await harness.start()

    # Start the autonomy background loops under the local profile — the SAME shared
    # runtime the REPL + API lifespan use. Crucially this is what runs the #6 surface-GC
    # ticker that prunes stale chat<->session bindings; without it GC would be dead on
    # the primary Telegram surface (the one that mints those bindings). Fail-open.
    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            _data_dir = getattr(getattr(container, "config", None), "data_dir", "data")
            autonomy_handles = start_autonomy(task_agent=task_agent, data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    # Greet: confirm which bot we're driving + the owner-lock state.
    try:
        me = await harness.bot.get_me()
        username = getattr(me, "username", None)
    except Exception:
        username = None
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

    # Clean shutdown on Ctrl-C: stop the loop, then close the bot session.
    loop = asyncio.get_running_loop()
    poll_task = asyncio.ensure_future(harness.run_polling())

    def _stop(*_a):
        harness._running = False
        poll_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError):
        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(_sig, lambda *_a: _stop())
            except (ValueError, OSError, AttributeError):
                pass  # SIGTERM may be unavailable on some platforms/threads

    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    finally:
        click.echo("\n" + click.style("stopping telegram bot…", dim=True))
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
