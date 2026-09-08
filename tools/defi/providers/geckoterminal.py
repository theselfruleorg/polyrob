"""GeckoTerminal — pool DISCOVERY (keyless). Proposal 029 R4.

`defi_data` could answer questions about a token you already knew about, but it
had no verb for "what launched in the last hour". So the prod agent did
discovery by hand: `web_fetch` against DexScreener's `token-profiles/latest` and
GeckoTerminal's `new_pools`, parsing the JSON in-context, every run. That is why
403s and "page params 400" ended up in its reports — it was scraping, not
calling a provider.

Parsing is kept strictly separate from fetching so tests never touch the
network, mirroring `providers/dexscreener.py`.

**Everything here is a CLAIM, and the claims are unusually weak.** These are
pools minutes old. Anyone can create one. Two failure modes are common enough to
be handled explicitly rather than left to the caller:

* **Reserves come back non-positive.** In one live page, 9 of 20 rows reported a
  negative `reserve_in_usd` — an indexing artifact on a pool the indexer has not
  caught up with. A non-positive reserve is UNKNOWN here, never `0.0`, because
  "$0 liquidity" and "the indexer has not indexed it" are different facts and
  only one of them is a reason to reject a token.
* **The base token may not be on the chain you asked about.** The `base_token`
  relationship id is prefixed with the network (`base_0x…`), so a row whose
  prefix disagrees is dropped rather than trusted.

This module ranks nothing and rejects nothing on merit. It returns what the
indexer says; the honeypot/sell-tax screens (`providers/goplus.py`) and the
caller's own liquidity floors do the deciding.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.geckoterminal.com/api/v2"
TIMEOUT_SEC = 12.0

name = "geckoterminal"


@dataclass(frozen=True)
class PoolCandidate:
    """One freshly-indexed pool. Never "a good token" — just a pool that exists."""
    chain: str
    pool_address: str
    #: The non-quote side, checksummed. ``None`` when the indexer did not name a
    #: token this chain's address rules accept — the row is still shown, because
    #: hiding it would misreport how much the scan actually saw.
    base_token: Optional[str]
    name: str
    dex: str
    created_at: Optional[str]
    #: ``None`` = the indexer gave no usable figure. NOT zero. See the module
    #: docstring: a negative reserve is an indexing artifact, not an empty pool.
    liquidity_usd: Optional[float]
    volume_h24_usd: Optional[float]
    price_change_h24_pct: Optional[float]


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _positive(value: Any) -> Optional[float]:
    """A figure that is only meaningful when positive; otherwise UNKNOWN."""
    out = _f(value)
    return out if (out is not None and out > 0) else None


def _base_token(rel: Dict[str, Any], gt_network: str, chain: str) -> Optional[str]:
    """The base token's address, or None.

    The id is ``<network>_<address>``. The network prefix is CHECKED rather than
    stripped blindly: a row for another chain would otherwise be reported as if
    it were on the one the caller asked about, and the same address is a
    different token on a different chain.
    """
    raw = (((rel or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
    prefix = f"{gt_network}_"
    if not raw.startswith(prefix):
        return None
    addr = raw[len(prefix):]
    # Family-dispatched: a Solana mint is base58 and would fail EIP-55, so an
    # EVM-only validator here silently blanked every Solana row.
    from core.wallet.addresses import normalize_for_chain
    try:
        return normalize_for_chain(chain, addr)
    except ValueError:
        logger.debug("geckoterminal: dropping malformed base token %r", addr)
        return None


def parse_pools(payload: Any, chain: str, gt_network: str) -> List[PoolCandidate]:
    """Pure. ``payload`` is the decoded JSON body; no network here."""
    rows = ((payload or {}).get("data") or []) if isinstance(payload, dict) else []
    out: List[PoolCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") or {}
        rel = row.get("relationships") or {}
        vol = attrs.get("volume_usd") or {}
        chg = attrs.get("price_change_percentage") or {}
        out.append(PoolCandidate(
            chain=chain,
            pool_address=str(attrs.get("address") or ""),
            base_token=_base_token(rel, gt_network, chain),
            name=str(attrs.get("name") or ""),
            dex=str(((rel.get("dex") or {}).get("data") or {}).get("id") or "unknown"),
            created_at=attrs.get("pool_created_at"),
            liquidity_usd=_positive(attrs.get("reserve_in_usd")),
            volume_h24_usd=_f(vol.get("h24")),
            price_change_h24_pct=_f(chg.get("h24")),
        ))
    return out


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": "polyrob-defi/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return json.loads(resp.read())


def _fetch(chain: str, endpoint: str, *, fetch=None) -> List[PoolCandidate]:
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.geckoterminal_id:
        # A chain this indexer does not cover returns NOTHING rather than
        # another chain's pools. "We could not look" is the honest answer.
        return []
    url = f"{BASE_URL}/networks/{row.geckoterminal_id}/{endpoint}"
    try:
        payload = (fetch or _get)(url)
    except Exception as exc:
        logger.info("geckoterminal: %s failed for %s (%s)", endpoint, chain, exc)
        return []
    return parse_pools(payload, chain, row.geckoterminal_id)


def new_pools(chain: str, *, fetch=None) -> List[PoolCandidate]:
    """Pools the indexer saw most recently — the fresh-launch frontier."""
    return _fetch(chain, "new_pools", fetch=fetch)


def trending_pools(chain: str, *, fetch=None) -> List[PoolCandidate]:
    """Pools the indexer currently ranks as trending. A popularity claim, and
    popularity is purchasable — treat it as a place to look, not a verdict."""
    return _fetch(chain, "trending_pools", fetch=fetch)


def health() -> bool:
    try:
        _get(f"{BASE_URL}/networks/base/new_pools")
        return True
    except Exception:
        return False
