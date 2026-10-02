"""``polyrob surfaces probe feishu``: the ``tenant_access_token`` read, then
``bot/v3/info`` (both reads). See ``surfaces/_probe.py``.

The token is never rendered: the detail is the bot's app name only."""
from surfaces._probe import ProbeResult, http_json, missing
from surfaces.feishu.client import api_base


async def probe(env) -> ProbeResult:
    gap = missing(env, "FEISHU_APP_ID", "FEISHU_APP_SECRET")
    if gap:
        return gap
    if (env.get("FEISHU_TRANSPORT") or "ws").strip().lower() == "webhook":
        from core.env import parse_bool
        from surfaces.feishu.webhook import UNSIGNED_OPT_IN, webhook_auth_gap
        why = webhook_auth_gap((env.get("FEISHU_ENCRYPT_KEY") or "").strip(),
                               (env.get("FEISHU_VERIFICATION_TOKEN") or "").strip(),
                               parse_bool(env.get(UNSIGNED_OPT_IN) or "", False))
        if why:
            return ProbeResult.failed(why)
    base = api_base(env.get("FEISHU_DOMAIN"))
    status, payload, err = await http_json(
        "POST", f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": env["FEISHU_APP_ID"].strip(),
              "app_secret": env["FEISHU_APP_SECRET"].strip()})
    if err:
        return ProbeResult.unavailable(err)
    if not (isinstance(payload, dict) and payload.get("code") == 0
            and payload.get("tenant_access_token")):
        return ProbeResult.failed(_reason(payload, status))
    token = str(payload["tenant_access_token"])
    status, info, err = await http_json(
        "GET", f"{base}/open-apis/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"})
    if err:
        return ProbeResult.ok("app credential accepted (bot info unavailable)")
    if isinstance(info, dict) and info.get("code") == 0:
        bot = info.get("bot") or {}
        return ProbeResult.ok(str(bot.get("app_name") or "?"))
    return ProbeResult.failed("token ok, bot info refused: " + _reason(info, status)
                              + " (enable the app's bot capability)")


def _reason(payload, status) -> str:
    if isinstance(payload, dict) and payload.get("code") is not None:
        return f"code {payload.get('code')} {payload.get('msg') or ''}".strip()
    return f"HTTP {status}"
