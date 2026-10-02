"""``polyrob surfaces probe dingtalk``: the app ``accessToken`` read.

The token is never rendered: the detail names only that the credential was
accepted and how long the token lives."""
from surfaces._probe import ProbeResult, http_json, missing
from surfaces.dingtalk.client import API_BASE


async def probe(env) -> ProbeResult:
    gap = missing(env, "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET")
    if gap:
        return gap
    status, payload, err = await http_json(
        "POST", f"{API_BASE}/v1.0/oauth2/accessToken",
        json={"appKey": env["DINGTALK_CLIENT_ID"].strip(),
              "appSecret": env["DINGTALK_CLIENT_SECRET"].strip()})
    if err:
        return ProbeResult.unavailable(err)
    if isinstance(payload, dict) and payload.get("accessToken"):
        ttl = payload.get("expireIn")
        return ProbeResult.ok("app credential accepted"
                              + (f" (token lives {ttl}s)" if ttl else ""))
    if isinstance(status, int) and status >= 500:
        return ProbeResult.unavailable(f"HTTP {status}")
    return ProbeResult.failed(_reason(payload, status))


def _reason(payload, status) -> str:
    if isinstance(payload, dict) and payload.get("code"):
        return f"code {payload.get('code')} {payload.get('message') or ''}".strip()
    return f"HTTP {status}"
