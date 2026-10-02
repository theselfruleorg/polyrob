"""``polyrob gateway`` launch for X (Twitter) DMs (polling). See ``surfaces/_launch.py``."""
import os

from surfaces._launch import LaunchContext, harness_launched

_OAUTH1 = ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
           "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")


async def launch(ctx: LaunchContext):
    missing = [k for k in _OAUTH1 if not (os.environ.get(k) or "").strip()]
    try:
        from polyrob_x.x_oauth2 import oauth2_configured
        oauth2 = oauth2_configured()
    except Exception:
        oauth2 = bool((os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip())
    if missing and not oauth2:
        ctx.warn("X_SURFACE_ENABLED=true but missing " + ", ".join(missing)
                 + " (set an OAuth 2.0 PKCE user token or all OAuth 1.0a "
                   "user-context keys). Skipping X.")
        return None
    from polyrob_x.surface.harness import build_x_harness
    return harness_launched(build_x_harness(ctx.container, ctx.task_agent,
                                            data_dir=ctx.data_dir))
