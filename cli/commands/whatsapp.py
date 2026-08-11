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
    from cli.commands._surface_runner import SurfaceJob, run_surface

    def _check_creds(container, task_agent):
        # Preflight Meta WhatsApp credentials — otherwise the worker prints "online" but
        # the verify handshake + every send fail later (404/401) with no local signal.
        missing = [v for v in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                               "WHATSAPP_VERIFY_TOKEN")
                   if not (os.environ.get(v) or "").strip()]
        if missing:
            click.echo(click.style("[polyrob] ERROR: ", fg="red")
                       + "WhatsApp not configured — set " + ", ".join(missing)
                       + " to run the webhook worker.")
            sys.exit(1)

    def _voice_signal(container):
        # One-line voice-readiness signal so a deploy with missing faster-whisper is visible.
        from core.surfaces.transcription import log_transcription_readiness
        log_transcription_readiness(container)

    async def _build(ctx):
        # Assemble the WhatsApp inbound/outbound harness and register on the container.
        from surfaces.whatsapp.harness import build_whatsapp_harness
        harness = build_whatsapp_harness(ctx.container, ctx.task_agent,
                                         data_dir=ctx.data_dir)

        # Wire the webhook router to this container, then serve via uvicorn.
        import uvicorn  # noqa: PLC0415 — intentionally lazy (keeps import-time clean)
        from fastapi import FastAPI
        from api.webhooks import router as webhooks_router, set_container_provider

        set_container_provider(lambda: ctx.container)
        app = FastAPI(title="polyrob whatsapp webhook worker")
        app.include_router(webhooks_router)

        config = uvicorn.Config(app, host="0.0.0.0", port=port,
                                log_level=ctx.log_level.lower())
        server = uvicorn.Server(config)

        async def _announce():
            click.echo(click.style("whatsapp webhook worker online", fg="green")
                       + f": listening on port {port}")
            click.echo(click.style(
                "configure Meta webhook URL to: http(s)://<host>/webhooks/whatsapp",
                dim=True))
            click.echo(click.style("Ctrl-C to stop", dim=True))

        def _graceful_exit():
            # Propagate SIGINT/SIGTERM into uvicorn's graceful shutdown.
            server.should_exit = True

        return SurfaceJob(harness=harness, run=server.serve, announce=_announce,
                          on_stop=_graceful_exit, cancel_on_stop=False,
                          guard_harness_stop=True)

    await run_surface(
        extra_env={"WHATSAPP_SURFACE_ENABLED": "true"},
        verbose=verbose,
        preflight=_check_creds,
        post_bus=_voice_signal,
        silence_loggers=("httpx", "httpcore", "asyncio", "hpack"),
        build_harness=_build,
        stopping_message="stopping whatsapp worker…",
    )
