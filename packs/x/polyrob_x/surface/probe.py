"""``polyrob surfaces probe x``: ``GET /2/users/me`` (a read). See ``surfaces/_probe.py``.

The OAuth 2.0 user token is read the way the agent reads it — through the managed
store resolver (``polyrob_x.x_oauth2.resolve_access_token``: encrypted store →
auto-refresh → the static ``TWITTER_OAUTH2_ACCESS_TOKEN`` override) — and proven
directly. Until 2026-09-26 the probe read ONLY the static env value, so it
ignored the store every consumer actually uses (P2-6). OAuth 1.0a keys need
request signing; that goes through tweepy (the ``twitter`` extra) when installed,
and is also the answer when the OAuth 2.0 login is refused."""
import asyncio

from surfaces._probe import ProbeResult, http_json, missing

_OAUTH1 = ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
           "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")


def _remedy() -> str:
    try:
        from polyrob_x.x_oauth2 import RELOGIN_REMEDY
        return RELOGIN_REMEDY
    except Exception:
        return "re-login with `/x login` or `polyrob x-account oauth-login`"


async def _stored_token() -> str:
    try:
        from polyrob_x.x_oauth2 import resolve_access_token
        return (await asyncio.wait_for(asyncio.to_thread(resolve_access_token),
                                       timeout=30) or "").strip()
    except Exception:
        return ""


async def probe(env) -> ProbeResult:
    token = await _stored_token() or (env.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip()
    oauth2_refused = ""
    if token:
        status, payload, err = await http_json(
            "GET", "https://api.twitter.com/2/users/me",
            headers={"Authorization": f"Bearer {token}"})
        if err:
            return ProbeResult.unavailable(err)
        if status == 200 and isinstance(payload, dict):
            return ProbeResult.ok("@" + str((payload.get("data") or {}).get("username") or "?"))
        oauth2_refused = f"HTTP {status}: the OAuth 2.0 token was refused — {_remedy()}"
        if missing(env, *_OAUTH1):
            return ProbeResult.failed(oauth2_refused)
    gap = missing(env, *_OAUTH1)
    if gap:
        return gap
    try:
        import tweepy
    except Exception:
        return ProbeResult.unavailable("tweepy not installed (pip install 'polyrob[twitter]')")

    def _me():
        client = tweepy.Client(consumer_key=env["TWITTER_API_KEY"].strip(),
                               consumer_secret=env["TWITTER_API_SECRET_KEY"].strip(),
                               access_token=env["TWITTER_ACCESS_TOKEN"].strip(),
                               access_token_secret=env["TWITTER_ACCESS_TOKEN_SECRET"].strip())
        return client.get_me(user_auth=True)

    try:
        resp = await asyncio.wait_for(asyncio.to_thread(_me), timeout=15)
    except Exception as e:
        return ProbeResult.failed(type(e).__name__ + (f"; {oauth2_refused}" if oauth2_refused else ""))
    data = getattr(resp, "data", None)
    handle = "@" + str(getattr(data, "username", "?"))
    if oauth2_refused:
        return ProbeResult.ok(f"{handle} via OAuth 1.0a (DM fallback); {oauth2_refused}")
    return ProbeResult.ok(handle)
