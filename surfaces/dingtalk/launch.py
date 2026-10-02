"""``polyrob gateway`` launch for DingTalk (Stream Mode). See ``surfaces/_launch.py``."""
import os

from surfaces._launch import LaunchContext, harness_launched


async def launch(ctx: LaunchContext):
    cid = (os.environ.get("DINGTALK_CLIENT_ID") or "").strip()
    secret = (os.environ.get("DINGTALK_CLIENT_SECRET") or "").strip()
    if not (cid and secret):
        missing = [n for n, v in (("DINGTALK_CLIENT_ID", cid),
                                  ("DINGTALK_CLIENT_SECRET", secret)) if not v]
        ctx.warn("DingTalk is enabled but missing " + ", ".join(missing)
                 + ". Skipping DingTalk.")
        return None
    from surfaces.dingtalk.harness import build_dingtalk_harness
    return harness_launched(build_dingtalk_harness(
        ctx.container, ctx.task_agent, client_id=cid, client_secret=secret,
        data_dir=ctx.data_dir))
