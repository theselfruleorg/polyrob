"""``polyrob gateway`` launch for Slack (Socket Mode). See ``surfaces/_launch.py``."""
import os

from surfaces._launch import LaunchContext, harness_launched


async def launch(ctx: LaunchContext):
    bot = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    app = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
    if not (bot and app):
        missing = [n for n, v in (("SLACK_BOT_TOKEN", bot), ("SLACK_APP_TOKEN", app)) if not v]
        ctx.warn("SLACK_SURFACE_ENABLED=true but missing " + ", ".join(missing)
                 + " (needs BOTH xoxb- and xapp- tokens). Skipping Slack.")
        return None
    from surfaces.slack.harness import build_slack_harness
    return harness_launched(build_slack_harness(
        ctx.container, ctx.task_agent, bot_token=bot, app_token=app, data_dir=ctx.data_dir))
