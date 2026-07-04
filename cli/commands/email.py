"""polyrob email — let the agent receive & reply to email via IMAP polling + SMTP.

v1 is CORRESPONDENT-ONLY: owner-by-email is OFF (a forgeable From: can never command
the agent), so an email sender is at most a correspondent the agent already contacted
(their reply -> DATA into the originating session) or DENIED. The agent contacts third
parties via the email tool; their replies flow back here.

Credentials: the existing gmail_email / gmail_app_password config (./.polyrob/.env or
config/.env.*). Nothing is committed.
"""
import asyncio
import os
import signal
import sys
from typing import Optional

import click


@click.command()
@click.option("--poll", default=None, type=int, help="IMAP poll seconds (else EMAIL_IMAP_POLL_SEC)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def email(poll: Optional[int], verbose: bool):
    """Run the agent as an email correspondent surface (IMAP poll + SMTP)."""
    asyncio.run(_run_email(poll, verbose))


async def _run_email(poll_opt: Optional[int], verbose: bool):
    import logging as _logging

    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat

    # Outbound bus + tier model gate ON for this command (explicit env still wins),
    # BEFORE the container build so the bus installs during TaskAgent construction.
    os.environ.setdefault("SINGULAR_CHAT_ENABLED", "true")
    os.environ.setdefault("EMAIL_SURFACE_ENABLED", "true")
    os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED", "true")

    setup_project_path()
    setup_sqlite_compat()

    # No usable provider key → clean canonical message + exit (daemon: no inline wizard).
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)

    # Logging policy mirrors telegram.py: verbose→DEBUG; headless→INFO; interactive→quiet.
    # Without the headless→INFO branch a systemd email daemon (stderr=journal, not a TTY)
    # ran with logging effectively off, so 'routed N message(s)' + errors never appeared.
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
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + "TaskAgent not available")
        sys.exit(1)

    # Preflight IMAP/SMTP credentials — otherwise the surface prints "online
    # (unconfigured)" and then silently retries the poll forever with no signal.
    gmail_email = getattr(container.config, "gmail_email", None)
    gmail_pw = getattr(container.config, "gmail_app_password", None)
    if not (gmail_email and gmail_pw):
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "email not configured — set gmail_email + gmail_app_password in "
                     "./.polyrob/.env (or config/.env.*) to run the email surface.")
        sys.exit(1)

    from core.surfaces.bootstrap import install_surface_bus
    install_surface_bus(container)  # db_path defaults to container.config.data_dir

    # Start the outbound dispatcher (gated on SINGULAR_CHAT_ENABLED; may be None when the
    # bus is disabled). Without this, when durable outbound is enabled correspondent
    # replies enqueue and NEVER send. Mirrors telegram.py: sync start(), async stop().
    dispatcher = container.get_service("outbound_dispatcher")
    if dispatcher is not None:
        dispatcher.start()

    # Register the correspondent registry on the container so the dispatcher can resolve
    # tiers + the harness can route correspondent replies to originating sessions.
    try:
        from core.surfaces.correspondents import CorrespondentRegistry
        _data_dir = getattr(getattr(container, "config", None), "data_dir", "data") or "data"
        if container.get_service("correspondent_registry") is None:
            container.register_service(
                "correspondent_registry",
                CorrespondentRegistry(os.path.join(_data_dir, "correspondents.db")),
            )
    except Exception as e:
        click.echo(click.style("[polyrob] WARN: ", fg="yellow")
                   + f"correspondent registry unavailable: {e}")

    # Build the email tool (SMTP send + IMAP config) from the container config.
    from tools.email_tool import EmailTool
    email_tool = EmailTool("email", container.config, container)

    from agents.task.surface_config import SurfaceConfig
    poll_sec = poll_opt if poll_opt is not None else SurfaceConfig.email_imap_poll_sec()

    from surfaces.email.harness import build_email_harness
    _data_dir = getattr(getattr(container, "config", None), "data_dir", "data") or "data"
    harness = build_email_harness(container, task_agent, email_tool=email_tool,
                                  data_dir=_data_dir, poll_interval=poll_sec)
    await harness.start()

    # Autonomy loops (surface GC etc) under the local profile — same shared runtime.
    autonomy_handles = None
    try:
        from agents.task.constants import local_mode_enabled
        if local_mode_enabled():
            from core.autonomy_runtime import start_autonomy
            autonomy_handles = start_autonomy(task_agent=task_agent, data_dir=_data_dir)
    except Exception:
        autonomy_handles = None

    addr = getattr(container.config, "gmail_email", None) or "(unconfigured)"
    click.echo(click.style("email surface online", fg="green") + f": {addr}")
    click.echo(click.style(
        "correspondent-only (owner-by-email is OFF). polling every "
        f"{poll_sec}s… (Ctrl-C to stop)", dim=True))

    loop = asyncio.get_running_loop()
    poll_task = asyncio.ensure_future(harness.run_polling())

    def _stop(*_a):
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
        click.echo("\n" + click.style("stopping email surface…", dim=True))
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
