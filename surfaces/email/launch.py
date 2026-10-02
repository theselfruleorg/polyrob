"""``polyrob gateway`` launch for email (IMAP poll + SMTP). See ``surfaces/_launch.py``."""
import os

from typing import Optional

from surfaces._launch import LaunchContext, Launched


def creds_error(config, env) -> Optional[str]:
    """None when the resolved email provider has what it needs; else the message.

    agentmail: only AGENTMAIL_API_KEY is required (the inbox self-provisions).
    smtp (legacy): gmail_email + gmail_app_password. The ONE check: `polyrob email`
    and `polyrob gateway` both run it (an unconfigured surface otherwise reports
    "online" and retries the poll forever with no signal).
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


async def launch(ctx: LaunchContext):
    err = creds_error(getattr(ctx.container, "config", None), os.environ)
    if err:
        ctx.warn(f"EMAIL_SURFACE_ENABLED=true but {err} Skipping Email.")
        return None
    try:
        from core.surfaces.correspondents import CorrespondentRegistry
        if ctx.container.get_service("correspondent_registry") is None:
            ctx.container.register_service(
                "correspondent_registry",
                CorrespondentRegistry(os.path.join(ctx.data_dir, "correspondents.db")),
            )
    except Exception as exc:
        ctx.warn(f"correspondent registry unavailable: {exc}")

    from core.surfaces.config import SurfaceConfig
    from tools.email_tool import EmailTool
    email_tool = EmailTool("email", ctx.container.config, ctx.container)
    poll_sec = SurfaceConfig.email_imap_poll_sec()

    from surfaces.email.harness import build_email_harness
    h = build_email_harness(ctx.container, ctx.task_agent, email_tool=email_tool,
                            data_dir=ctx.data_dir, poll_interval=poll_sec)
    await h.start()
    from core.instance import resolve_agent_email
    addr = resolve_agent_email() or "(unconfigured)"
    ctx.note(f"email polling {addr} every {poll_sec}s")
    return Launched(run=h.run_polling, stop=h.stop)
