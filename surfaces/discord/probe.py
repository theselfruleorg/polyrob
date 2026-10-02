"""``polyrob surfaces probe discord``: ``GET /users/@me`` (a read). See ``surfaces/_probe.py``."""
from surfaces._probe import ProbeResult, http_json, missing


async def probe(env) -> ProbeResult:
    gap = missing(env, "DISCORD_BOT_TOKEN")
    if gap:
        return gap
    status, payload, err = await http_json(
        "GET", "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {env['DISCORD_BOT_TOKEN'].strip()}"})
    if err:
        return ProbeResult.unavailable(err)
    if status == 200 and isinstance(payload, dict):
        return ProbeResult.ok(str(payload.get("username") or "?"))
    return ProbeResult.failed(f"HTTP {status}: the token was refused")
