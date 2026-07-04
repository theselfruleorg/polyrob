"""polyrob whatsapp — run the agent as a WhatsApp Cloud API bot (webhook server worker).

Inbound arrives via an HTTP webhook (Meta Cloud API POST to /webhooks/whatsapp); this
command builds the CLI container, installs the surface bus, assembles the WhatsApp harness,
and serves the FastAPI webhook app on --port via uvicorn.  No long-polling — the Meta
platform calls us.

Environment required (at runtime, not import time):
    WHATSAPP_ACCESS_TOKEN   — Meta permanent/system-user access token
    WHATSAPP_PHONE_NUMBER_ID — Meta Phone Number ID (sender)
    WHATSAPP_VERIFY_TOKEN   — echoed back on the GET verify handshake
    WHATSAPP_WEBHOOK_SECRET — (optional) HMAC-SHA256 payload-signing secret

Set WHATSAPP_SURFACE_ENABLED=true and SINGULAR_CHAT_ENABLED=true, or let the command
default them for you (explicit env wins).
"""
import asyncio
import os
import signal
import sys

import click


@click.command()
@click.option("--port", default=8080, show_default=True,
              help="Port to serve the /webhooks/whatsapp endpoint on")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def whatsapp(port: int, verbose: bool):
    """Run the agent as a WhatsApp Cloud API bot (webhook server)."""
    asyncio.run(_run_whatsapp(port, verbose))


async def _run_whatsapp(port: int, verbose: bool) -> None:
    import logging as _logging

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    # Default surface flags ON for this command — the whole point is WhatsApp.
    # Explicit env values still win (setdefault never clobbers).
    os.environ.setdefault("WHATSAPP_SURFACE_ENABLED", "true")
    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    # No usable provider key → clean canonical message + exit (daemon: no inline wizard).
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    # Logging policy mirrors telegram.py: verbose→DEBUG; headless→INFO; interactive→quiet.
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
        for _noisy in ("httpx", "httpcore", "asyncio", "hpack"):
            _logging.getLogger(_noisy).setLevel(_logging.WARNING)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + "TaskAgent not available in container")
        sys.exit(1)

    # Preflight Meta WhatsApp credentials — otherwise the worker prints "online" but
    # the verify handshake + every send fail later (404/401) with no local signal.
    missing = [v for v in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN")
               if not (os.environ.get(v) or "").strip()]
    if missing:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "WhatsApp not configured — set " + ", ".join(missing)
                   + " to run the webhook worker.")
        sys.exit(1)

    # Install the outbound surface bus (idempotent, gated SINGULAR_CHAT_ENABLED).
    from core.surfaces.bootstrap import install_surface_bus
    install_surface_bus(container)  # db_path defaults to container.config.data_dir

    # One-line voice-readiness signal so a deploy with missing faster-whisper is visible.
    from core.surfaces.transcription import log_transcription_readiness
    log_transcription_readiness(container)

    # Assemble the WhatsApp inbound/outbound harness and register on the container.
    # Pin harness state DBs (dedup / window) to the container's data_dir so per-instance
    # isolation holds under POLYROB_DATA_DIR (else they land in ./data).
    from surfaces.whatsapp.harness import build_whatsapp_harness
    _data_dir = getattr(getattr(container, "config", None), "data_dir", "data") or "data"
    harness = build_whatsapp_harness(container, task_agent, data_dir=_data_dir)

    # Start the outbound delivery dispatcher (if installed by the bus).
    dispatcher = container.get_service("outbound_dispatcher")
    if dispatcher is not None:
        dispatcher.start()

    # Start the autonomy background loops (cron/goals/curator) under the local profile —
    # same shared runtime the REPL and API lifespan use.
    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            _data_dir = getattr(getattr(container, "config", None), "data_dir", "data")
            autonomy_handles = start_autonomy(task_agent=task_agent, data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    # Wire the webhook router to this container, then serve via uvicorn.
    import uvicorn  # noqa: PLC0415 — intentionally lazy (keeps import-time clean)
    from fastapi import FastAPI
    from api.webhooks import router as webhooks_router, set_container_provider

    set_container_provider(lambda: container)
    app = FastAPI(title="polyrob whatsapp webhook worker")
    app.include_router(webhooks_router)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level=log_level.lower())
    server = uvicorn.Server(config)

    # Propagate SIGINT/SIGTERM into uvicorn's graceful shutdown.
    loop = asyncio.get_running_loop()

    def _stop(*_a):
        server.should_exit = True

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError):
        for _sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(_sig, lambda *_a: _stop())
            except (ValueError, OSError, AttributeError):
                pass  # SIGTERM may be unavailable on some platforms/threads

    click.echo(click.style("whatsapp webhook worker online", fg="green")
               + f": listening on port {port}")
    click.echo(click.style(
        "configure Meta webhook URL to: http(s)://<host>/webhooks/whatsapp", dim=True))
    click.echo(click.style("Ctrl-C to stop", dim=True))

    try:
        await server.serve()
    finally:
        click.echo("\n" + click.style("stopping whatsapp worker…", dim=True))
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
        try:
            await harness.stop()
        except Exception:
            pass
