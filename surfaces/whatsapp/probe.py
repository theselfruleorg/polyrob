"""``polyrob surfaces probe whatsapp``: read the phone-number object from the Graph
API (a read). See ``surfaces/_probe.py``."""
from surfaces._probe import ProbeResult, http_json, missing


async def probe(env) -> ProbeResult:
    gap = missing(env, "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")
    if gap:
        return gap
    status, payload, err = await http_json(
        "GET", f"https://graph.facebook.com/v21.0/{env['WHATSAPP_PHONE_NUMBER_ID'].strip()}"
               "?fields=display_phone_number",
        headers={"Authorization": f"Bearer {env['WHATSAPP_ACCESS_TOKEN'].strip()}"})
    if err:
        return ProbeResult.unavailable(err)
    if status == 200 and isinstance(payload, dict):
        note = "" if (env.get("WHATSAPP_WEBHOOK_SECRET") or "").strip() else \
            " (WHATSAPP_WEBHOOK_SECRET unset: inbound is refused)"
        return ProbeResult.ok(str(payload.get("display_phone_number") or "?") + note)
    return ProbeResult.failed(f"HTTP {status}: the token or number id was refused")
