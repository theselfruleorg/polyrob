"""``polyrob surfaces probe signal``: the signal-cli daemon's ``listAccounts``
(a read, local). See ``surfaces/_probe.py``."""
from surfaces._probe import ProbeResult, http_json, missing


async def probe(env) -> ProbeResult:
    gap = missing(env, "SIGNAL_ACCOUNT")
    if gap:
        return gap
    daemon = (env.get("SIGNAL_DAEMON_URL") or "http://127.0.0.1:8080").strip().rstrip("/")
    status, payload, err = await http_json(
        "POST", f"{daemon}/api/v1/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "listAccounts"})
    if err:
        return ProbeResult.unavailable(f"signal-cli daemon unreachable ({err})")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, list):
        return ProbeResult.failed(f"HTTP {status}: no account list from the daemon")
    account = env["SIGNAL_ACCOUNT"].strip()
    numbers = {str(a.get("number") if isinstance(a, dict) else a) for a in result}
    if account in numbers:
        return ProbeResult.ok(f"{account} linked")
    return ProbeResult.failed(f"{account} is not linked in this signal-cli daemon")
