"""``polyrob surfaces probe telegram``: Bot API ``getMe`` (a read). See ``surfaces/_probe.py``."""
from surfaces._probe import ProbeResult, http_json, missing


async def probe(env) -> ProbeResult:
    gap = missing(env, "TELEGRAM_BOT_TOKEN")
    if gap:
        return gap
    status, payload, err = await http_json(
        "GET", f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN'].strip()}/getMe")
    if err:
        return ProbeResult.unavailable(err)
    if isinstance(payload, dict) and payload.get("ok"):
        return ProbeResult.ok("@" + str((payload.get("result") or {}).get("username") or "?"))
    return ProbeResult.failed(f"HTTP {status}: the token was refused")
