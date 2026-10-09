"""``polyrob gateway`` launch for WhatsApp (Meta webhook). See ``surfaces/_launch.py``.

Building the harness is what registers ``webhook_surfaces['whatsapp']`` — without
it every Meta verify/inbound POST 404s while the gateway claims the surface is
online. The gateway's shared webhook server carries the HTTP side.
"""
import os

from surfaces._launch import LaunchContext, Launched

#: CHAT-22: the webhook secret is REQUIRED — without it every inbound POST is
#: refused (fail-closed but silent), so the surface would look online and hear nothing.
_REQUIRED = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
             "WHATSAPP_WEBHOOK_SECRET")


async def launch(ctx: LaunchContext):
    # Preflight the Meta credentials — otherwise the webhook serves but the verify
    # handshake and every send fail later (401/404) with no local signal.
    missing = [v for v in _REQUIRED if not (os.environ.get(v) or "").strip()]
    if missing:
        ctx.warn("WHATSAPP_SURFACE_ENABLED=true but missing "
                 + ", ".join(missing) + ". Skipping WhatsApp.")
        return None
    from surfaces.whatsapp.harness import build_whatsapp_harness
    h = build_whatsapp_harness(ctx.container, ctx.task_agent, data_dir=ctx.data_dir)
    ctx.note(f"whatsapp webhook: configure Meta to POST to "
             f"http(s)://<host>:{ctx.port}/webhooks/whatsapp")
    return Launched(stop=h.stop, webhook=True)
