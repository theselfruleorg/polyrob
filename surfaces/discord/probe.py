"""``polyrob surfaces probe discord``: ``GET /users/@me`` and
``GET /applications/@me`` (reads). See ``surfaces/_probe.py``.

The gateway IDENTIFYs with the MESSAGE_CONTENT privileged intent; a bot that
lacks it is closed with 4014 on every connect. The second read checks the
application flags so the probe says so before the gateway does.
"""
from surfaces._probe import ProbeResult, http_json, missing

_API = "https://discord.com/api/v10"
#: Application flags that grant MESSAGE_CONTENT (verified / unverified bot).
_MESSAGE_CONTENT_FLAGS = (1 << 18) | (1 << 19)


async def probe(env) -> ProbeResult:
    gap = missing(env, "DISCORD_BOT_TOKEN")
    if gap:
        return gap
    headers = {"Authorization": f"Bot {env['DISCORD_BOT_TOKEN'].strip()}"}
    status, payload, err = await http_json("GET", f"{_API}/users/@me", headers=headers)
    if err:
        return ProbeResult.unavailable(err)
    if not (status == 200 and isinstance(payload, dict)):
        return ProbeResult.failed(f"HTTP {status}: the token was refused")
    name = str(payload.get("username") or "?")
    status, app, err = await http_json("GET", f"{_API}/applications/@me", headers=headers)
    if err:
        return ProbeResult.unavailable(err)
    if status != 200 or not isinstance(app, dict):
        return ProbeResult.unavailable(f"could not read the application flags (HTTP {status})")
    try:
        flags = int(app.get("flags") or 0)
    except (TypeError, ValueError):
        flags = 0
    if not flags & _MESSAGE_CONTENT_FLAGS:
        return ProbeResult.failed(
            f"{name}: the MESSAGE_CONTENT privileged intent is off — enable it in "
            "the Discord Developer Portal (Bot > Privileged Gateway Intents), or "
            "the gateway closes with 4014")
    return ProbeResult.ok(name)
