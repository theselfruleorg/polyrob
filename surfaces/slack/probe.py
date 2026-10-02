"""``polyrob surfaces probe slack``: ``auth.test`` (a read). See ``surfaces/_probe.py``.

Only the bot token is proven here; the app-level token (``xapp-``) is proven by
the Socket Mode connection itself, which this read cannot open without starting
the surface."""
from surfaces._probe import ProbeResult, http_json, missing


async def probe(env) -> ProbeResult:
    gap = missing(env, "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
    if gap:
        return gap
    status, payload, err = await http_json(
        "POST", "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {env['SLACK_BOT_TOKEN'].strip()}"})
    if err:
        return ProbeResult.unavailable(err)
    if isinstance(payload, dict) and payload.get("ok"):
        return ProbeResult.ok(f"{payload.get('user') or '?'} in {payload.get('team') or '?'}")
    reason = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
    return ProbeResult.failed(str(reason))
