"""Read-only x402 discovery — probe an endpoint, sweep many, score payability.

**NEVER PAYS. NEVER NEEDS A WALLET.** No payment header is ever sent; this
module only reads what a 402 challenge says.

Why this is core and not a script
---------------------------------
POLYROB ships an agent with a wallet (``x402_pay``) and an invoice rail
(``x402_invoice``). Before it can transact it has to answer three questions:
*does this endpoint charge, how much, and can I actually pay it?* Nothing in
core answered them — ``x402_quote`` prices ONE known URL, needs a wallet to be
enabled, and is GET-only. So the prod instance hand-built a probe/sweep/score
CLI in its project directory, and the daily workspace GC deleted it. Any
capability every deployment needs, and that an instance would otherwise rebuild
by hand, belongs here.

The challenge decode is NOT reimplemented here. It delegates to
``RealX402Client._decode_challenge`` / ``_normalize_accepts`` /
``_parse_402_challenge`` so there is exactly ONE x402 challenge parser in the
codebase and a fix to it reaches the payer and the prober alike.

Honesty rules kept from the throwaway original:
  * an unparseable 402 body is reported as unparseable, never guessed;
  * a missing price is ``None``, never ``0.0``;
  * a network failure is an ``error`` string on the row, never an exception
    that aborts a sweep.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 12
MAX_SWEEP_TARGETS = 50
MAX_SWEEP_CONCURRENCY = 8
# A 402 challenge is a small JSON body. Cap the read so one agent-supplied URL
# returning a multi-GB body (or a gzip bomb) can't OOM the worker — mirrors
# web_fetch's incremental byte cap; a sweep amplifies an unbounded read 50x.
MAX_PROBE_BYTES = 2_097_152  # 2 MiB — generous for a challenge, far below OOM

# A probe is a plain read. Nothing here may ever carry payment authorization.
_PROBE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "polyrob-x402-probe (read-only; never pays)",
}


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score_endpoint(row: dict) -> tuple[int, list[str]]:
    """Rate how payable an endpoint actually is, 0-5, with the reasons it lost.

    +1 answered at all          (not dead)
    +1 answered HTTP 402        (it charges)
    +1 challenge body parseable (it says HOW)
    +1 price disclosed          (it says HOW MUCH)
    +1 full payment routing     (asset AND network AND payTo)

    5 means an agent could pay it today. Anything less names what is missing, so
    the gap is actionable instead of a bare number.
    """
    score = 0
    reasons: list[str] = []

    if row.get("error") or row.get("status") is None:
        return 0, ["endpoint did not answer"]
    score += 1

    if row.get("status") != 402:
        reasons.append(f"not a paid endpoint (HTTP {row.get('status')})")
        return score, reasons
    score += 1

    if not row.get("challenge_parseable"):
        reasons.append("402 with no parseable challenge body")
        return score, reasons
    score += 1

    if row.get("price_usd") is None:
        reasons.append("no price disclosed in the challenge")
    else:
        score += 1

    if row.get("asset") and row.get("network") and row.get("pay_to"):
        score += 1
    else:
        reasons.append("incomplete payment routing (needs asset + network + payTo)")

    return score, reasons


def is_payable(row: dict) -> bool:
    """True only when an agent could actually pay this endpoint right now."""
    return row.get("score") == 5


# ---------------------------------------------------------------------------
# the network seam
# ---------------------------------------------------------------------------

async def _httpx_fetch(url, *, method="GET", body=None, timeout=None, headers=None,
                       pinned_ip=None):
    """Default fetch: one plain request, no redirects followed, no payment.

    The connection is pinned to ``pinned_ip`` (the IP the SSRF validator already
    resolved and cleared) so a DNS rebind between validate and connect cannot
    redirect the socket to an internal address — the same defense web_fetch uses
    (``tools/web_fetch/fetcher.py::_PinnedResolver``). The read is bounded to
    ``MAX_PROBE_BYTES`` and the whole request to a TOTAL timeout so a hostile
    body (giant / gzip bomb / slow-drip) can't OOM or hang the worker.
    """
    import aiohttp
    from urllib.parse import urlparse
    from tools.web_fetch.fetcher import _PinnedResolver

    total = float(timeout or DEFAULT_TIMEOUT)
    hostname = urlparse(url).hostname
    if pinned_ip and hostname:
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(hostname, pinned_ip))
    else:
        connector = aiohttp.TCPConnector()

    data = body.encode("utf-8") if isinstance(body, str) else body
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.request(
            method, url, data=data, headers=headers or _PROBE_HEADERS,
            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=total),
        ) as resp:
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(8192):
                buf.extend(chunk)
                if len(buf) > MAX_PROBE_BYTES:
                    raise ValueError(
                        f"probe response exceeds {MAX_PROBE_BYTES} bytes")
            # Hand the parser an httpx.Response — the ONE challenge decoder
            # (RealX402Client) reads httpx-shaped .headers/.status_code/.text/.json().
            import httpx
            return httpx.Response(
                status_code=resp.status,
                headers=[(k, v) for k, v in resp.headers.items()],
                content=bytes(buf),
            )


def _default_validator():
    """The shared SSRF validator — the agent supplies these URLs.

    Same guard ``web_fetch`` uses, so cloud metadata (169.254.169.254) and
    RFC1918 stay shut on a surface that takes an arbitrary URL from the model.
    """
    from tools.mcp.security import get_url_validator
    return get_url_validator(allow_http=True)


async def _validate(url: str, validator) -> tuple[Optional[str], Optional[str]]:
    """``(refusal, pinned_ip)`` — refusal is None when the URL may be probed.

    ``pinned_ip`` is the validator-cleared IP the fetch must connect to (defeats
    a DNS rebind between here and connect); it is 127.0.0.1 for the agent's own
    published sandbox port (the one narrow loopback exception, exactly as in
    web_fetch — never RFC1918, never metadata) and None only when validation was
    skipped by that exception.
    """
    try:
        from tools.shell.loopback_allow import is_loopback_allowed
        if is_loopback_allowed(url):
            return None, "127.0.0.1"
    except Exception:
        pass
    try:
        # validate_and_resolve does a BLOCKING socket.getaddrinfo; offload it so a
        # slow/sinkhole DNS can't freeze the event loop for the resolver timeout.
        ok, err, pinned = await asyncio.get_running_loop().run_in_executor(
            None, validator.validate_and_resolve, url)
    except Exception as exc:  # a broken validator must not open the gate
        return f"blocked URL (validator error: {exc})", None
    return (None, pinned) if ok else (f"blocked URL ({err})", None)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def _empty_row(url: str, method: str) -> dict:
    return {"url": url, "method": method.upper(), "status": None, "error": None,
            "challenge_parseable": False, "price_usd": None, "network": None,
            "asset": None, "pay_to": None, "accepts": []}


async def probe_endpoint(
    url: str,
    *,
    method: str = "GET",
    body: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    fetch=None,
    validator=None,
) -> dict:
    """Probe ONE endpoint read-only and score it. Never raises for HTTP status.

    ``fetch`` and ``validator`` are injectable seams so tests stay fully offline.
    """
    method = (method or "GET").upper()
    row = _empty_row(url, method)

    validator = validator if validator is not None else _default_validator()
    refusal, pinned_ip = await _validate(url, validator)
    if refusal:
        row["error"] = refusal
        row["score"], row["score_reasons"] = 0, [refusal]
        return row

    fetch = fetch or _httpx_fetch
    try:
        kwargs = {"method": method, "body": body, "timeout": timeout}
        # Only the default fetch pins the IP; injected test seams don't take it.
        pin_kwargs = {"pinned_ip": pinned_ip} if fetch is _httpx_fetch else {}
        try:
            resp = await fetch(url, headers=dict(_PROBE_HEADERS), **kwargs, **pin_kwargs)
        except TypeError:
            # Injected seams may not accept `headers`; the default one does.
            resp = await fetch(url, **kwargs)
    except Exception as exc:  # DNS, TLS, timeout — honest, never fatal
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["score"], row["score_reasons"] = score_endpoint(row)
        return row

    row["status"] = getattr(resp, "status_code", None)

    if row["status"] == 402:
        from tools.x402.real_client import RealX402Client

        parsed = RealX402Client._parse_402_challenge(resp)
        if parsed and not parsed.get("_unparseable"):
            row["challenge_parseable"] = True
            row["price_usd"] = parsed.get("amount")
            row["network"] = parsed.get("network")
            row["asset"] = parsed.get("asset")
            row["pay_to"] = parsed.get("pay_to")
        try:
            decoded = RealX402Client._decode_challenge(resp)
            for entry in RealX402Client._normalize_accepts(decoded or {}):
                if not isinstance(entry, dict):
                    continue
                asset = (entry.get("assetName") or entry.get("symbol")
                         or entry.get("asset") or entry.get("token") or "?")
                if isinstance(asset, dict):
                    asset = asset.get("symbol") or asset.get("address") or "?"
                row["accepts"].append({
                    "network": entry.get("network") or entry.get("chain") or "?",
                    "asset": asset,
                    "scheme": entry.get("scheme") or entry.get("paymentScheme") or "?",
                })
        except Exception:
            # A broken challenge is already reflected by challenge_parseable.
            logger.debug("probe: could not summarise accepts for %s", url)

    row["score"], row["score_reasons"] = score_endpoint(row)
    return row


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

def _outcome_key(row: dict) -> str:
    if row.get("error") or row.get("status") is None:
        return "dead"
    status = row["status"]
    if status == 402:
        return "http_402"
    if status == 200:
        return "http_200_open"
    return "http_other"


async def sweep_endpoints(
    targets,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    fetch=None,
    validator=None,
) -> dict:
    """Probe many endpoints and return one scored ledger.

    ``targets`` accepts bare URL strings or ``{service, url, method?, body?}``
    dicts. Bounded by MAX_SWEEP_TARGETS and run at MAX_SWEEP_CONCURRENCY so one
    call can never turn into an unbounded outbound scan.
    """
    targets = list(targets or [])
    if len(targets) > MAX_SWEEP_TARGETS:
        raise ValueError(
            f"sweep accepts at most {MAX_SWEEP_TARGETS} targets, got {len(targets)}")

    normalized = []
    for t in targets:
        if isinstance(t, str):
            normalized.append({"service": t, "url": t})
        elif isinstance(t, dict) and t.get("url"):
            normalized.append({"service": t.get("service") or t["url"], **t})
        else:
            raise ValueError(f"target must be a URL string or a dict with 'url': {t!r}")

    validator = validator if validator is not None else _default_validator()
    gate = asyncio.Semaphore(MAX_SWEEP_CONCURRENCY)

    async def _one(target):
        async with gate:
            row = await probe_endpoint(
                target["url"], method=target.get("method", "GET"),
                body=target.get("body"), timeout=timeout,
                fetch=fetch, validator=validator)
            row["service"] = target["service"]
            return row

    rows = await asyncio.gather(*(_one(t) for t in normalized))

    distribution: dict[str, int] = {}
    for row in rows:
        key = _outcome_key(row)
        distribution[key] = distribution.get(key, 0) + 1

    return {
        "swept": len(rows),
        "outcome_distribution": distribution,
        "payable": sum(1 for r in rows if is_payable(r)),
        "endpoints": list(rows),
    }
