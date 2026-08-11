"""polyrob email — let the agent receive & reply to email via IMAP polling + SMTP.

v1 is CORRESPONDENT-ONLY: owner-by-email is OFF (a forgeable From: can never command
the agent), so an email sender is at most a correspondent the agent already contacted
(their reply -> DATA into the originating session) or DENIED. The agent contacts third
parties via the email tool; their replies flow back here.

Credentials: the existing gmail_email / gmail_app_password config (./.polyrob/.env or
config/.env.*). Nothing is committed.
"""
import asyncio
import logging
import os
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
    from cli.commands._surface_runner import SurfaceJob, run_surface

    def _check_creds(container, task_agent):
        # Preflight IMAP/SMTP credentials — otherwise the surface prints "online
        # (unconfigured)" and then silently retries the poll forever with no signal.
        gmail_email = getattr(container.config, "gmail_email", None)
        gmail_pw = getattr(container.config, "gmail_app_password", None)
        if not (gmail_email and gmail_pw):
            click.echo(click.style("[polyrob] ERROR: ", fg="red")
                       + "email not configured — set gmail_email + gmail_app_password in "
                         "./.polyrob/.env (or config/.env.*) to run the email surface.")
            sys.exit(1)

    def _autonomy_precheck() -> bool:
        # Autonomy loops are OFF here by default (proposal 010, option A). Both systemd
        # entrypoints used to call start_autonomy against the SAME goals.db/cron.db, so a
        # telegram-outbound goal claimed by this email-only process (whose MessageRouter
        # has no telegram surface) deterministically could not send. The telegram process
        # is the single autonomy driver; EMAIL_AUTONOMY_RUNTIME=true restores the legacy
        # dual-runtime behavior. SMTP outbound (cron deliver=email / the agent's
        # send_email tool) is credential-driven and does NOT need this runtime.
        from core.env import bool_env
        if not bool_env("EMAIL_AUTONOMY_RUNTIME", False):
            logging.getLogger(__name__).info(
                "autonomy runtime disabled in email process (EMAIL_AUTONOMY_RUNTIME=off)")
            return False
        return True

    async def _build(ctx):
        container, task_agent, data_dir = ctx.container, ctx.task_agent, ctx.data_dir

        # Register the correspondent registry on the container so the dispatcher can
        # resolve tiers + the harness can route correspondent replies to originating
        # sessions.
        try:
            from core.surfaces.correspondents import CorrespondentRegistry
            if container.get_service("correspondent_registry") is None:
                container.register_service(
                    "correspondent_registry",
                    CorrespondentRegistry(os.path.join(data_dir, "correspondents.db")),
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
        harness = build_email_harness(container, task_agent, email_tool=email_tool,
                                      data_dir=data_dir, poll_interval=poll_sec)
        await harness.start()

        async def _announce():
            addr = getattr(container.config, "gmail_email", None) or "(unconfigured)"
            click.echo(click.style("email surface online", fg="green") + f": {addr}")
            click.echo(click.style(
                "correspondent-only (owner-by-email is OFF). polling every "
                f"{poll_sec}s… (Ctrl-C to stop)", dim=True))

        return SurfaceJob(harness=harness, run=harness.run_polling, announce=_announce)

    await run_surface(
        extra_env={"EMAIL_SURFACE_ENABLED": "true",
                   "CORRESPONDENT_ACCESS_ENABLED": "true"},
        verbose=verbose,
        preflight=_check_creds,
        silence_loggers=("httpx", "httpcore", "asyncio", "hpack"),
        autonomy_precheck=_autonomy_precheck,
        build_harness=_build,
        stopping_message="stopping email surface…",
    )
