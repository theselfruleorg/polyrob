"""``polyrob gateway`` launch for Feishu / Lark. See ``surfaces/_launch.py``.

Two transports (``FEISHU_TRANSPORT``): ``ws`` (default — the lark-oapi long
connection, no public URL, needs the ``[feishu]`` extra) or ``webhook`` (order
0003 — the gateway's ``/webhooks/feishu`` route, bare install, needs an Encrypt
Key; a Verification Token alone needs ``FEISHU_WEBHOOK_ALLOW_UNSIGNED=true``).
"""
import os

from core.optional_extras import extra_available, pip_hint
from surfaces._launch import LaunchContext, Launched, harness_launched


async def launch(ctx: LaunchContext):
    app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
    secret = (os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if not (app_id and secret):
        missing = [n for n, v in (("FEISHU_APP_ID", app_id), ("FEISHU_APP_SECRET", secret))
                   if not v]
        ctx.warn("Feishu is enabled but missing " + ", ".join(missing) + ". Skipping Feishu.")
        return None
    transport = (os.environ.get("FEISHU_TRANSPORT") or "ws").strip().lower()
    domain = os.environ.get("FEISHU_DOMAIN")
    if transport == "webhook":
        encrypt_key = (os.environ.get("FEISHU_ENCRYPT_KEY") or "").strip()
        token = (os.environ.get("FEISHU_VERIFICATION_TOKEN") or "").strip()
        from core.env import bool_env
        from surfaces.feishu.webhook import UNSIGNED_OPT_IN, webhook_auth_gap
        allow_unsigned = bool_env(UNSIGNED_OPT_IN, False)
        gap = webhook_auth_gap(encrypt_key, token, allow_unsigned)
        if gap:
            ctx.warn(gap + ". Skipping Feishu.")
            return None
        if not encrypt_key:
            ctx.warn(f"feishu webhook: {UNSIGNED_OPT_IN}=true — events are NOT signed; "
                     "only the static Verification Token authenticates them.")
        from surfaces.feishu.harness import build_feishu_webhook
        _hook, client = await build_feishu_webhook(
            ctx.container, ctx.task_agent, app_id=app_id, app_secret=secret,
            domain=domain, encrypt_key=encrypt_key, verification_token=token,
            data_dir=ctx.data_dir, allow_unsigned=allow_unsigned)
        ctx.note(f"feishu webhook: set the app's event request URL to "
                 f"https://<host>/webhooks/feishu (gateway port {ctx.port})")
        return Launched(stop=client.close, webhook=True)
    if transport != "ws":
        ctx.warn(f"FEISHU_TRANSPORT={transport!r} is not ws|webhook. Skipping Feishu.")
        return None
    if not extra_available("feishu"):
        ctx.warn(f"Feishu needs the [feishu] extra (lark-oapi) — {pip_hint('feishu')}. "
                 "Skipping Feishu.")
        return None
    from surfaces.feishu.harness import build_feishu_harness
    return harness_launched(build_feishu_harness(
        ctx.container, ctx.task_agent, app_id=app_id, app_secret=secret,
        domain=domain, data_dir=ctx.data_dir))
