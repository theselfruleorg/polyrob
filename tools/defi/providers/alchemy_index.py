"""Alchemy — holdings enumeration (optional, requires a key).

EVM has no RPC that lists what an address holds. DexScreener prices a token you
name; GoPlus screens a token you name; neither enumerates. So a complete
portfolio needs an indexer, and without one `portfolio` must say it scanned a
candidate list rather than implying completeness.

Reuses the existing ``ALCHEMY_API_KEY`` (today used only for NFT ownership in
`tools/alchemy/`). Absent key = this provider is simply unavailable; it is never
a hard requirement.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

name = "alchemy_index"

_BASE_URL = "https://base-mainnet.g.alchemy.com/v2"


def base_url_for(chain: str):
    """Alchemy endpoint for *chain*, or None when it has no slug.

    None is the honest answer: guessing a slug would enumerate holdings on the
    WRONG network and report them as this chain's.
    """
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.alchemy_slug:
        return None
    return f"https://{row.alchemy_slug}.g.alchemy.com/v2"


def available() -> bool:
    return bool(os.getenv("ALCHEMY_API_KEY", "").strip())


def parse_balances(payload: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """{checksummed address -> raw units} for non-zero holdings.

    Entries carrying an ``error`` are UNKNOWN and are dropped — an unknown
    balance must never be rendered as a holding, and certainly never as a zero.
    Zero balances are returned by the API for tokens ever touched; they are not
    holdings.
    """
    from core.wallet.tokens import normalize_address

    if not payload or not isinstance(payload, dict):
        return {}
    result = payload.get("result") or {}
    entries = result.get("tokenBalances") or []
    out: Dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("error"):
            continue
        raw = entry.get("tokenBalance")
        addr = entry.get("contractAddress")
        if not raw or not addr:
            continue
        try:
            value = int(raw, 16)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        try:
            out[normalize_address(addr)] = value
        except ValueError:
            logger.debug("alchemy_index: dropping malformed address %r", addr)
    return out


# --------------------------------------------------------------------------
# Network boundary — never exercised by unit tests.
# --------------------------------------------------------------------------

def fetch_balances(holder: str, timeout: float = 10.0, *,
                   chain: str = "base") -> Optional[Dict[str, int]]:
    """Holdings for *holder* on *chain*, or None when unavailable/failed.

    None means "no answer" — the caller must fall back to a candidate-list scan
    and label the coverage partial, never report an empty portfolio.
    """
    key = os.getenv("ALCHEMY_API_KEY", "").strip()
    if not key:
        return None
    base_url = base_url_for(chain)
    if not base_url:
        return None
    # ⚠️ The credential is in the URL PATH, so a name-keyed secret scrubber
    # never sees it and `exc_info=True` publishes it verbatim — an httpx
    # HTTPStatusError renders "... for url '<the whole keyed URL>'". The same
    # pattern leaked a real key to the production journal from the x402
    # settlement scan. Log the REDACTED endpoint, and scrub the key out of the
    # exception text rather than printing a traceback that may embed it.
    url = f"{base_url}/{key}"
    try:
        import httpx
        r = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "alchemy_getTokenBalances",
                  "params": [holder]},
            timeout=timeout)
        if r.status_code != 200:
            return None
        return parse_balances(r.json())
    except Exception as exc:
        from core.security.redaction import redact_url, scrub_secret
        logger.debug("alchemy_index: fetch failed for %s via %s: %s", holder,
                     redact_url(url),
                     scrub_secret(f"{type(exc).__name__}: {exc}", key))
        return None
