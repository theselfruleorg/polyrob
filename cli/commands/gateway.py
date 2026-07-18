"""polyrob gateway — run ALL enabled surfaces in ONE process.

Builds the container once and then starts each enabled surface concurrently:
  - Telegram long-polling  (TELEGRAM_SURFACE_ENABLED)
  - WhatsApp webhook       (WHATSAPP_SURFACE_ENABLED)
  - Email IMAP poll        (EMAIL_SURFACE_ENABLED)
  - Discord gateway WS     (DISCORD_SURFACE_ENABLED)
  - Slack Socket Mode      (SLACK_SURFACE_ENABLED)
  - Signal signal-cli SSE  (SIGNAL_SURFACE_ENABLED)
  - X (Twitter) DM polling (X_SURFACE_ENABLED)

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
              help="Port for the WhatsApp (and any HTTP-based) webhook surface")
@click.option("--telegram-token", default=None, envvar="TELEGRAM_BOT_TOKEN",
              help="Telegram bot token (else TELEGRAM_BOT_TOKEN env)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def gateway(port: int, telegram_token, verbose: bool):
    """Run all enabled surfaces (Telegram/WhatsApp/Email/Discord/Slack/Signal/X) in one process."""
    asyncio.run(_run_gateway(port, telegram_token, verbose))


async def _run_gateway(port: int, telegram_token_opt, verbose: bool) -> None:
    import logging as _logging
    logger = _logging.getLogger(__name__)

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    # Default the shared bus + correspondent access ON. The individual surface flags
    # (TELEGRAM_/WHATSAPP_/EMAIL_SURFACE_ENABLED) are NOT defaulted here — the operator
    # opts into which surfaces to run (see the "No surfaces enabled" guidance below);
    # this is the "run all ENABLED surfaces" launcher, not "force every surface on".
    # Explicit env values still win (os.environ.setdefault never clobbers).
    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    # No usable provider key → clean canonical message + exit (daemon: no inline wizard).
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

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
        dispatcher.start()

    # --- Autonomy background loops (cron / goals / curator / surface GC) ---
    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            _data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))
            autonomy_handles = start_autonomy(task_agent=task_agent, data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    # --- Resolve which surfaces are enabled ---
    from agents.task.surface_config import SurfaceConfig

    tg_enabled = SurfaceConfig.telegram_surface_enabled()
    wa_enabled = SurfaceConfig.whatsapp_surface_enabled()
    em_enabled = SurfaceConfig.email_surface_enabled()
    dc_enabled = SurfaceConfig.discord_surface_enabled()
    sl_enabled = SurfaceConfig.slack_surface_enabled()
    sg_enabled = SurfaceConfig.signal_surface_enabled()
    x_enabled = SurfaceConfig.x_surface_enabled()

    if not (tg_enabled or wa_enabled or em_enabled
            or dc_enabled or sl_enabled or sg_enabled or x_enabled):
        click.echo(click.style("[gateway] ", fg="yellow")
                   + "No surfaces enabled. Set at least one of:\n"
                     "  TELEGRAM_SURFACE_ENABLED=true\n"
                     "  WHATSAPP_SURFACE_ENABLED=true\n"
                     "  EMAIL_SURFACE_ENABLED=true\n"
                     "  DISCORD_SURFACE_ENABLED=true\n"
                     "  SLACK_SURFACE_ENABLED=true\n"
                     "  SIGNAL_SURFACE_ENABLED=true\n"
                     "  X_SURFACE_ENABLED=true")
        # Clean up what we started before bailing.
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except Exception:
                pass
        if autonomy_handles is not None:
            try:
                await autonomy_handles.stop()
            except Exception:
                pass
        return

    enabled_names = []
    if tg_enabled:
        enabled_names.append("telegram")
    if wa_enabled:
        enabled_names.append(f"whatsapp(:{port})")
    if em_enabled:
        enabled_names.append("email")
    if dc_enabled:
        enabled_names.append("discord")
    if sl_enabled:
        enabled_names.append("slack")
    if sg_enabled:
        enabled_names.append("signal")
    if x_enabled:
        enabled_names.append("x")
    click.echo(click.style("gateway online", fg="green")
               + ": " + ", ".join(enabled_names))

    # ---- Collect surface tasks / harnesses ----
    coroutines = []
    tg_harness = None
    wa_server = None
    wa_harness = None
    em_harness = None
    # Discord/Slack/Signal/X harnesses (uniform run()/stop() contract) — collected
    # for the shutdown sweep in `finally`.
    connector_harnesses = []

    def _run_harness(h):
        async def _run():
            try:
                await h.run()
            except asyncio.CancelledError:
                pass
        return _run()

    # --- Telegram ---
    if tg_enabled:
        tok = (telegram_token_opt or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not tok:
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + "TELEGRAM_SURFACE_ENABLED=true but no token "
                         "(set --telegram-token or TELEGRAM_BOT_TOKEN). "
                         "Skipping Telegram.")
        else:
            try:
                from surfaces.telegram.harness import build_telegram_harness
                _tg_data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))
                tg_harness = build_telegram_harness(container, task_agent, token=tok,
                                                    webhook_base=None, data_dir=_tg_data_dir)
                await tg_harness.start()

                async def _run_tg():
                    try:
                        await tg_harness.run_polling()
                    except asyncio.CancelledError:
                        pass

                coroutines.append(_run_tg())
            except Exception as exc:
                tg_harness = None
                click.echo(click.style("[gateway] WARN: ", fg="yellow")
                           + f"Telegram surface failed to start, skipping: {exc}")

    # --- WhatsApp ---
    # Preflight Meta WhatsApp credentials — otherwise the gateway serves the webhook
    # but the verify handshake + every send fail later (401/404) with no local signal.
    # Mirrors whatsapp.py's preflight; downgraded to WARN+skip (gateway skips a broken
    # surface rather than aborting the whole multi-surface process, like the tg-token case).
    wa_missing = [v for v in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN")
                  if not (os.environ.get(v) or "").strip()] if wa_enabled else []
    if wa_enabled and wa_missing:
        click.echo(click.style("[gateway] WARN: ", fg="yellow")
                   + "WHATSAPP_SURFACE_ENABLED=true but missing "
                   + ", ".join(wa_missing) + ". Skipping WhatsApp.")
    elif wa_enabled:
        try:
            import uvicorn  # noqa: PLC0415 — intentionally lazy
            from fastapi import FastAPI
            from api.webhooks import router as webhooks_router, set_container_provider
            from surfaces.whatsapp.harness import build_whatsapp_harness

            # CRITICAL: assemble + register the harness — this is what registers
            # webhook_surfaces['whatsapp']. Without it every Meta verify/inbound POST
            # 404s (the endpoint has no surface to route to), even though we serve it.
            # Pin state DBs (dedup / window) to the container's data_dir for isolation.
            _wa_data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))
            wa_harness = build_whatsapp_harness(container, task_agent, data_dir=_wa_data_dir)

            set_container_provider(lambda: container)
            wa_app = FastAPI(title="polyrob gateway — whatsapp webhook")
            wa_app.include_router(webhooks_router)

            wa_config = uvicorn.Config(wa_app, host="0.0.0.0", port=port,
                                       log_level=log_level.lower())
            wa_server = uvicorn.Server(wa_config)

            async def _run_wa():
                try:
                    await wa_server.serve()
                except asyncio.CancelledError:
                    pass

            coroutines.append(_run_wa())
            click.echo(click.style(
                f"  whatsapp webhook: configure Meta to POST to http(s)://<host>:{port}/webhooks/whatsapp",
                dim=True))
        except Exception as exc:
            wa_harness = None
            wa_server = None
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + f"WhatsApp surface failed to start, skipping: {exc}")

    # --- Email ---
    if em_enabled:
        _data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))
        try:
            from core.surfaces.correspondents import CorrespondentRegistry
            if container.get_service("correspondent_registry") is None:
                container.register_service(
                    "correspondent_registry",
                    CorrespondentRegistry(os.path.join(_data_dir, "correspondents.db")),
                )
        except Exception as exc:
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + f"correspondent registry unavailable: {exc}")

        try:
            from tools.email_tool import EmailTool
            email_tool = EmailTool("email", container.config, container)
            poll_sec = SurfaceConfig.email_imap_poll_sec()

            from surfaces.email.harness import build_email_harness
            em_harness = build_email_harness(container, task_agent, email_tool=email_tool,
                                             data_dir=_data_dir, poll_interval=poll_sec)
            await em_harness.start()
            addr = getattr(container.config, "gmail_email", None) or "(unconfigured)"
            click.echo(click.style(f"  email polling {addr} every {poll_sec}s", dim=True))

            async def _run_em():
                try:
                    await em_harness.run_polling()
                except asyncio.CancelledError:
                    pass

            coroutines.append(_run_em())
        except Exception as exc:
            em_harness = None
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + f"Email surface failed to start, skipping: {exc}")

    _conn_data_dir = data_dir_or_home(getattr(getattr(container, "config", None), "data_dir", None))

    # --- Discord ---
    if dc_enabled:
        dc_tok = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
        if not dc_tok:
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + "DISCORD_SURFACE_ENABLED=true but no DISCORD_BOT_TOKEN. "
                         "Skipping Discord.")
        else:
            try:
                from surfaces.discord.harness import build_discord_harness
                h = build_discord_harness(container, task_agent, token=dc_tok,
                                          data_dir=_conn_data_dir)
                connector_harnesses.append(h)
                coroutines.append(_run_harness(h))
            except Exception as exc:
                click.echo(click.style("[gateway] WARN: ", fg="yellow")
                           + f"Discord surface failed to start, skipping: {exc}")

    # --- Slack ---
    if sl_enabled:
        sl_bot = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
        sl_app = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
        if not (sl_bot and sl_app):
            missing = [n for n, v in (("SLACK_BOT_TOKEN", sl_bot), ("SLACK_APP_TOKEN", sl_app)) if not v]
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + "SLACK_SURFACE_ENABLED=true but missing "
                       + ", ".join(missing) + " (needs BOTH xoxb- and xapp- tokens). "
                         "Skipping Slack.")
        else:
            try:
                from surfaces.slack.harness import build_slack_harness
                h = build_slack_harness(container, task_agent, bot_token=sl_bot,
                                        app_token=sl_app, data_dir=_conn_data_dir)
                connector_harnesses.append(h)
                coroutines.append(_run_harness(h))
            except Exception as exc:
                click.echo(click.style("[gateway] WARN: ", fg="yellow")
                           + f"Slack surface failed to start, skipping: {exc}")

    # --- Signal ---
    if sg_enabled:
        sg_daemon = (os.environ.get("SIGNAL_DAEMON_URL") or "http://127.0.0.1:8080").strip()
        sg_account = (os.environ.get("SIGNAL_ACCOUNT") or "").strip()
        if not sg_account:
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + "SIGNAL_SURFACE_ENABLED=true but no SIGNAL_ACCOUNT "
                         "(the +E164 number linked in signal-cli). Skipping Signal.")
        else:
            try:
                from surfaces.signal.harness import build_signal_harness
                h = build_signal_harness(container, task_agent, daemon_url=sg_daemon,
                                         account=sg_account, data_dir=_conn_data_dir)
                connector_harnesses.append(h)
                coroutines.append(_run_harness(h))
            except Exception as exc:
                click.echo(click.style("[gateway] WARN: ", fg="yellow")
                           + f"Signal surface failed to start, skipping: {exc}")

    # --- X (Twitter) DMs ---
    if x_enabled:
        _x_required = ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
                       "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")
        x_missing = [k for k in _x_required if not (os.environ.get(k) or "").strip()]
        if x_missing:
            click.echo(click.style("[gateway] WARN: ", fg="yellow")
                       + "X_SURFACE_ENABLED=true but missing " + ", ".join(x_missing)
                       + " (DMs need OAuth 1.0a user-context keys). Skipping X.")
        else:
            try:
                from surfaces.x.harness import build_x_harness
                h = build_x_harness(container, task_agent, data_dir=_conn_data_dir)
                connector_harnesses.append(h)
                coroutines.append(_run_harness(h))
            except Exception as exc:
                click.echo(click.style("[gateway] WARN: ", fg="yellow")
                           + f"X surface failed to start, skipping: {exc}")

    if not coroutines:
        # All surfaces were enabled in flags but none produced a runnable coroutine
        # (e.g. Telegram flag set but no token).
        click.echo(click.style("[gateway] WARN: ", fg="yellow")
                   + "No surfaces could be started (check tokens / credentials).")
        if dispatcher is not None:
            try:
                await dispatcher.stop()
            except Exception:
                pass
        if autonomy_handles is not None:
            try:
                await autonomy_handles.stop()
            except Exception:
                pass
        return

    click.echo(click.style("Ctrl-C to stop", dim=True))

    # --- Signal handling: cancel all running tasks ---
    loop = asyncio.get_running_loop()
    gather_task = asyncio.ensure_future(asyncio.gather(*coroutines, return_exceptions=True))

    def _stop(*_a):
        if tg_harness is not None:
            tg_harness._running = False
        if wa_server is not None:
            wa_server.should_exit = True
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
        if tg_harness is not None:
            try:
                await tg_harness.stop()
            except Exception:
                pass
        if em_harness is not None:
            try:
                await em_harness.stop()
            except Exception:
                pass
        if wa_harness is not None:
            try:
                await wa_harness.stop()
            except Exception:
                pass
        for h in connector_harnesses:
            try:
                await h.stop()
            except Exception:
                pass
        # WhatsApp server shuts itself down via should_exit; no explicit stop needed.
