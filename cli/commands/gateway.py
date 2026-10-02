"""polyrob gateway — run ALL enabled surfaces in ONE process.

Builds the container once and then starts every surface in the surface catalog
(``core/surfaces/catalog.py``) whose ``*_SURFACE_ENABLED`` flag is on, each
through its own ``surfaces/<id>/launch.py`` (the contract: ``surfaces/_launch.py``).
Webhook surfaces share ONE HTTP server on ``--port`` (``/webhooks/<id>``).

All surfaces share the same TaskAgent, outbound dispatcher, surface bus, and
autonomy runtime, so cross-surface outbound routing works out of the box.
An enabled surface with missing credentials is WARNED about and skipped —
never silently ignored (H2, 2026-07-14 review).

If no surface flag is enabled the command prints a helpful message and exits.

Heavy deps (uvicorn, FastAPI, aiogram, etc.) are imported INSIDE _run_gateway
so this module loads cleanly without any surface env being set.
"""
import asyncio
import os
import signal
import sys

import click
from core.runtime_paths import data_dir_or_home


@click.command()
@click.option("--port", default=8080, show_default=True,
              help="Port for the shared webhook server (WhatsApp and any webhook surface)")
@click.option("--host", default="0.0.0.0", show_default=True,
              help="Bind address for the webhook server (127.0.0.1 behind a proxy)")
@click.option("--telegram-token", default=None, envvar="TELEGRAM_BOT_TOKEN",
              help="Telegram bot token (else TELEGRAM_BOT_TOKEN env)")
@click.option("--skip", default="", metavar="IDS",
              help="Comma list of surfaces to leave to their own process "
                   "(e.g. telegram,email when those run as separate services)")
@click.option("--no-autonomy", is_flag=True,
              help="Do not start the cron/goal/curator loops here (another process owns them)")
@click.option("--idle-when-empty", is_flag=True,
              help="With nothing to run, stay up idle instead of exiting (a service "
                   "unit then restarts with every deploy and picks up a new surface)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def gateway(port: int, host: str, telegram_token, skip: str, no_autonomy: bool,
            idle_when_empty: bool, verbose: bool):
    """Run every enabled chat surface in one process."""
    skip_ids = {s.strip().lower() for s in (skip or "").split(",") if s.strip()}
    asyncio.run(_run_gateway(port, telegram_token, verbose, host=host, skip=skip_ids,
                             autonomy=not no_autonomy, idle_when_empty=idle_when_empty))


def _enabled_specs(skip=frozenset()) -> list:
    """Catalog rows this gateway runs: enabled (flag, or configured = on) and not
    left to another process."""
    from core.surfaces.catalog import surfaces as _catalog_surfaces
    from core.surfaces.config import SurfaceConfig
    return [spec for spec in _catalog_surfaces()
            if spec.id not in skip and SurfaceConfig.surface_enabled(spec.id)]


def _withheld_enabled(skip=frozenset()) -> list:
    """``(row, reason)`` for pack surfaces whose flag is on but whose pack did
    not load (067 P3b): named, never silently absent."""
    from core.surfaces.catalog import withheld_surfaces
    from core.env import bool_env
    return [(spec, why) for spec, why in withheld_surfaces()
            if spec.id not in skip and bool_env(spec.enabled_flag, False)]


async def _run_gateway(port: int, telegram_token_opt, verbose: bool, *, host: str = "0.0.0.0",
                       skip=frozenset(), autonomy: bool = True,
                       idle_when_empty: bool = False) -> None:
    import logging as _logging
    logger = _logging.getLogger(__name__)

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    setup_project_path()
    setup_sqlite_compat()

    # No usable provider key → clean canonical message + exit (daemon: no inline wizard).
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    # Default the shared bus + correspondent access ON. The individual surface flags
    # (TELEGRAM_/WHATSAPP_/EMAIL_SURFACE_ENABLED) are NOT defaulted here — the operator
    # opts into which surfaces to run (see the "No surfaces enabled" guidance below);
    # this is the "run all ENABLED surfaces" launcher, not "force every surface on".
    # Explicit env OR FILE values still win: the setdefault runs AFTER the
    # preflight's load_env (026 P1.3 — the old before-load order made load_env's
    # override=False silently ignore a file-set CORRESPONDENT_ACCESS_ENABLED=false).
    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED", "true")

    # Nothing to run → decide BEFORE the container build (which costs hundreds of
    # MB): a service unit idles cheaply; an interactive run explains and exits.
    if idle_when_empty and not _enabled_specs(skip):
        click.echo(click.style("[gateway] ", fg="yellow")
                   + "no chat surface is configured here yet — idling until the next "
                     "restart (a deploy, or after `polyrob surfaces add <id>`).")
        await asyncio.Event().wait()
        return

    # Logging: verbose→DEBUG; headless→INFO; interactive→quiet (mirrors telegram.py).
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
        click.echo(click.style("[gateway] ERROR: ", fg="red") + f"failed to start: {e}")
        sys.exit(1)
    if quiet:
        _logging.disable(_logging.NOTSET)
    elif not verbose:
        for _noisy in ("httpx", "httpcore", "aiogram.event", "asyncio", "hpack"):
            _logging.getLogger(_noisy).setLevel(_logging.WARNING)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[gateway] ERROR: ", fg="red") + "TaskAgent not available in container")
        sys.exit(1)

    # --- Shared bus (idempotent; gated on SINGULAR_CHAT_ENABLED) ---
    from core.surfaces.bootstrap import install_surface_bus
    install_surface_bus(container)  # db_path defaults to container.config.data_dir

    # --- Voice-readiness signal (fast-whisper availability) ---
    try:
        from core.surfaces.transcription import log_transcription_readiness
        log_transcription_readiness(container)
    except Exception:
        pass

    # --- Outbound dispatcher ---
    dispatcher = container.get_service("outbound_dispatcher")
    if dispatcher is not None:
        from cli.commands._bootstrap import attach_dispatcher_event_log
        attach_dispatcher_event_log(dispatcher)
        dispatcher.start()

    # --- Autonomy background loops (cron / goals / curator / surface GC) ---
    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if autonomy and local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            _data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))
            autonomy_handles = start_autonomy(task_agent=task_agent, data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    # --- Resolve which surfaces are enabled (064 F1: from the surface catalog) ---
    from core.surfaces.catalog import surfaces as _catalog_surfaces
    from core.surfaces.config import SurfaceConfig

    # 067 P3b: a pack's surface row (e.g. X from the x pack) runs only while its
    # pack loaded; phase 2 is idempotent (the container build above ran it).
    from core.packs.loader import load_packs
    load_packs()
    enabled = _enabled_specs(skip)
    for _spec, _why in _withheld_enabled(skip):
        click.echo(click.style("[gateway] ", fg="yellow") + f"{_spec.id}: not started — {_why}")

    async def _stop_runtime():
        """Clean up what we started before bailing / on shutdown."""
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

    if not enabled:
        from core.remedy import flag_command
        click.echo(click.style("[gateway] ", fg="yellow")
                   + "No surfaces enabled. Enable at least one (takes effect: restart):\n"
                   + "\n".join(f"  {flag_command(spec.enabled_flag)}"
                               for spec in _catalog_surfaces()))
        await _stop_runtime()
        return

    click.echo(click.style("gateway online", fg="green") + ": " + ", ".join(
        f"{spec.id}(:{port})" if spec.transport == "webhook" else spec.id
        for spec in enabled))

    def _warn(msg: str) -> None:
        click.echo(click.style("[gateway] WARN: ", fg="yellow") + msg)

    def _note(msg: str) -> None:
        click.echo(click.style(f"  {msg}", dim=True))

    def _skip_reason(exc: BaseException) -> str:
        """Skip-line detail; a missing optional dep gains the pip-extra remedy."""
        from core.optional_extras import missing_extra_hint

        hint = missing_extra_hint(str(exc))
        return f"{exc} ({hint})" if hint else str(exc)

    # ---- Launch each enabled surface through its package's launch(ctx) ----
    # An enabled surface with missing credentials WARNs and is skipped; one that
    # raises is skipped too — never a crash of the whole multi-surface process.
    import importlib

    from surfaces._launch import LaunchContext
    ctx = LaunchContext(
        container=container, task_agent=task_agent,
        data_dir=data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None)),
        port=port, warn=_warn, note=_note,
        options={"telegram_token": telegram_token_opt})
    launched = []   # (spec, Launched)
    for spec in enabled:
        try:
            mod = importlib.import_module(f"{spec.module}.launch")
            got = await mod.launch(ctx)
        except Exception as exc:
            _warn(f"{spec.label} surface failed to start, skipping: {_skip_reason(exc)}")
            continue
        if got is not None:
            launched.append((spec, got))

    coroutines = []

    def _run(fn):
        async def _go():
            try:
                await fn()
            except asyncio.CancelledError:
                pass
        return _go()

    for _spec, got in launched:
        if got.run is not None:
            coroutines.append(_run(got.run))

    # --- ONE webhook server for every webhook surface (/webhooks/<surface_id>) ---
    web_server = None
    webhook_specs = [spec for spec, got in launched if got.webhook]
    if webhook_specs:
        try:
            import uvicorn  # noqa: PLC0415 — intentionally lazy
            from fastapi import FastAPI
            from api.webhooks import router as webhooks_router, set_container_provider

            set_container_provider(lambda: container)
            web_app = FastAPI(title="polyrob gateway — webhooks")
            web_app.include_router(webhooks_router)
            web_server = uvicorn.Server(uvicorn.Config(
                web_app, host=host, port=port, log_level=log_level.lower()))
            # The replay finishes BEFORE the server accepts a request: its reset of
            # `processing` rows must only ever touch rows a DEAD process held.
            from surfaces._launch import recover_webhook_surfaces
            await recover_webhook_surfaces(container, task_agent, note=_note, warn=_warn)
            coroutines.append(_run(web_server.serve))
        except Exception as exc:
            names = ", ".join(spec.label for spec in webhook_specs)
            _warn(f"webhook server failed to start, skipping {names}: {_skip_reason(exc)}")
            web_server = None

    if not coroutines:
        # Every enabled surface was skipped (e.g. Telegram flag set but no token).
        _warn("No surfaces could be started (check tokens / credentials).")
        for _spec, got in launched:
            if got.stop is not None:
                try:
                    await got.stop()
                except Exception:
                    pass
        await _stop_runtime()
        return

    click.echo(click.style("Ctrl-C to stop", dim=True))

    # --- Signal handling: cancel all running tasks ---
    loop = asyncio.get_running_loop()
    gather_task = asyncio.ensure_future(asyncio.gather(*coroutines, return_exceptions=True))

    def _stop(*_a):
        for _spec, got in launched:
            if got.on_signal is not None:
                try:
                    got.on_signal()
                except Exception:
                    pass
        if web_server is not None:
            web_server.should_exit = True
        gather_task.cancel()

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
        results = await gather_task
        for res in (results or []):
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                logger.error("gateway: a surface exited with an error: %r", res)
    except asyncio.CancelledError:
        pass
    finally:
        click.echo("\n" + click.style("stopping gateway…", dim=True))
        await _stop_runtime()
        for _spec, got in launched:
            if got.stop is not None:
                try:
                    await got.stop()
                except Exception:
                    pass
        # The webhook server shuts itself down via should_exit; no explicit stop needed.
