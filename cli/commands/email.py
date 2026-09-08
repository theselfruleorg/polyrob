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


def _creds_error(config, env) -> Optional[str]:
    """None when the resolved email provider has what it needs; else the message.

    agentmail: only AGENTMAIL_API_KEY is required (the inbox self-provisions).
    smtp (legacy): gmail_email + gmail_app_password, unchanged.
    """
    from core.config_policy.policy import email_provider
    if email_provider(env) == "agentmail":
        if not (env.get("AGENTMAIL_API_KEY") or "").strip():
            return ("EMAIL_PROVIDER=agentmail but AGENTMAIL_API_KEY is unset — "
                    "set it in ./.polyrob/.env (or config/.env.*).")
        return None
    gmail_email = getattr(config, "gmail_email", None)
    gmail_pw = getattr(config, "gmail_app_password", None)
    if not (gmail_email and gmail_pw):
        return ("email not configured — set gmail_email + gmail_app_password in "
                "./.polyrob/.env (or config/.env.*) to run the email surface, or "
                "set AGENTMAIL_API_KEY for a self-provisioned managed inbox.")
    return None


@click.command()
@click.option("--poll", default=None, type=int, help="IMAP poll seconds (else EMAIL_IMAP_POLL_SEC)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logging")
def email(poll: Optional[int], verbose: bool):
    """Run the agent as an email correspondent surface (IMAP poll + SMTP)."""
    asyncio.run(_run_email(poll, verbose))


async def _run_email(poll_opt: Optional[int], verbose: bool):
    from cli.commands._surface_runner import SurfaceJob, run_surface

    def _check_creds(container, task_agent):
        # Preflight per-provider credentials — otherwise the surface prints "online
        # (unconfigured)" and then silently retries the poll forever with no signal.
        err = _creds_error(container.config, os.environ)
        if err:
            click.echo(click.style("[polyrob] ERROR: ", fg="red") + err)
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

        from core.surfaces.config import SurfaceConfig
        poll_sec = poll_opt if poll_opt is not None else SurfaceConfig.email_imap_poll_sec()

        from surfaces.email.harness import build_email_harness
        harness = build_email_harness(container, task_agent, email_tool=email_tool,
                                      data_dir=data_dir, poll_interval=poll_sec)
        await harness.start()

        async def _announce():
            from core.config_policy.policy import email_provider
            from core.instance import resolve_agent_email
            provider = email_provider()
            if provider == "agentmail":
                # Provision eagerly so the announce shows the real address and a
                # bad API key fails loudly at startup, not on the first poll.
                try:
                    await email_tool.ensure_initialized()
                except Exception as e:
                    click.echo(click.style("[polyrob] WARN: ", fg="yellow")
                               + f"agentmail provisioning failed: {e}")
            addr = resolve_agent_email() or "(unconfigured)"
            click.echo(click.style("email surface online", fg="green")
                       + f": {addr} [{provider}]")
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
