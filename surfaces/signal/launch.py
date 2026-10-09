"""``polyrob gateway`` launch for Signal (signal-cli SSE). See ``surfaces/_launch.py``."""
import os

from surfaces._launch import LaunchContext, harness_launched


async def launch(ctx: LaunchContext):
    daemon = (os.environ.get("SIGNAL_DAEMON_URL") or "http://127.0.0.1:8080").strip()
    account = (os.environ.get("SIGNAL_ACCOUNT") or "").strip()
    if not account:
        ctx.warn("SIGNAL_SURFACE_ENABLED=true but no SIGNAL_ACCOUNT "
                 "(the +E164 number linked in signal-cli). Skipping Signal.")
        return None
    from surfaces.signal.client import daemon_url_problem
    problem = daemon_url_problem(daemon)
    if problem:
        ctx.warn(f"{problem}. Skipping Signal.")
        return None
    from surfaces.signal.harness import build_signal_harness
    return harness_launched(build_signal_harness(
        ctx.container, ctx.task_agent, daemon_url=daemon, account=account,
        data_dir=ctx.data_dir))
