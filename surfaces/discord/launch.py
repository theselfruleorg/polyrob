"""``polyrob gateway`` launch for Discord (gateway WS). See ``surfaces/_launch.py``."""
import os

from surfaces._launch import LaunchContext, harness_launched


async def launch(ctx: LaunchContext):
    tok = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not tok:
        ctx.warn("DISCORD_SURFACE_ENABLED=true but no DISCORD_BOT_TOKEN. Skipping Discord.")
        return None
    from surfaces.discord.harness import build_discord_harness
    return harness_launched(build_discord_harness(
        ctx.container, ctx.task_agent, token=tok, data_dir=ctx.data_dir))
