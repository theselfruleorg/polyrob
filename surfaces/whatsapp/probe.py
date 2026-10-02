"""``polyrob surfaces probe whatsapp``: read the phone-number object from the Graph
API (a read). See ``surfaces/_probe.py``.

OS14: the probe is not ``ok`` while inbound is dead. Without
``WHATSAPP_WEBHOOK_SECRET`` every delivery is refused, and without
``WHATSAPP_VERIFY_TOKEN`` Meta's verify handshake fails — both are named as
missing, like any other absent credential.
"""
from surfaces._probe import ProbeResult, http_json, missing

_KEYS = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
         "WHATSAPP_WEBHOOK_SECRET", "WHATSAPP_VERIFY_TOKEN")


async def probe(env) -> ProbeResult:
    gap = missing(env, *_KEYS)
    if gap:
        return gap
    status, payload, err = await http_json(
        "GET", f"https://graph.facebook.com/v21.0/{env['WHATSAPP_PHONE_NUMBER_ID'].strip()}"
               "?fields=display_phone_number",
        headers={"Authorization": f"Bearer {env['WHATSAPP_ACCESS_TOKEN'].strip()}"})
    if err:
        return ProbeResult.unavailable(err)
    if status == 200 and isinstance(payload, dict):
        return ProbeResult.ok(str(payload.get("display_phone_number") or "?"))
    return ProbeResult.failed(f"HTTP {status}: the token or number id was refused")
