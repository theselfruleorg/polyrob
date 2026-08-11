"""DexScreener — price, liquidity and symbol candidate discovery (keyless).

Parsing is kept strictly separate from fetching so tests never touch the
network. Everything here is a CLAIM: DexScreener indexes public pools, and
anyone can create a pool, so both the price and the liquidity behind it are
attacker-influenceable. `parse_search` therefore never picks a winner — it
returns every contract claiming the ticker, ranked, and the caller must choose
an address.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tools.defi.providers.base import Candidate, PriceInfo, confidence_for

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens"

name = "dexscreener"


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _checksum(addr: str) -> Optional[str]:
    """Checksum a provider-supplied address, or None if it is malformed.

    A provider returning a bad checksum is misbehaving, so the candidate is
    dropped rather than trusted — but never silently, so a schema change or a
    hostile response is diagnosable.
    """
    from core.wallet.tokens import normalize_address
    try:
        return normalize_address(addr)
    except ValueError as exc:
        logger.debug("dexscreener: dropping malformed address %r (%s)", addr, exc)
        return None


#: Chains this tier can identify. DexScreener also indexes non-EVM chains
#: (Solana et al.) whose addresses are base58, not 20-byte hex. Filtering is
#: EXPLICIT rather than an accident of address validation, so that "we only
#: searched Base" is a statement the caller can make honestly.
SUPPORTED_CHAINS = ("base",)


def parse_search(payload: Optional[Dict[str, Any]],
                 chains=SUPPORTED_CHAINS) -> List[Candidate]:
    """Every distinct contract on *chains* claiming the searched ticker.

    Ranking is a display convenience, NOT a safety signal — liquidity is
    purchasable, so a typosquat with a seeded pool can outrank the real token.
    That is exactly why this returns a list and the caller must name an address.

    Candidates on unsupported chains are dropped deliberately (see
    SUPPORTED_CHAINS); the tool reports which chains it searched.
    """
    if not payload:
        return []
    allowed = set(chains) if chains else None
    best: Dict[tuple, Candidate] = {}
    for pair in payload.get("pairs") or []:
        base = pair.get("baseToken") or {}
        raw_addr = base.get("address")
        chain = pair.get("chainId")
        if not raw_addr or not chain:
            continue
        if allowed is not None and chain not in allowed:
            continue
        addr = _checksum(raw_addr)
        if addr is None:
            continue
        liq = _f((pair.get("liquidity") or {}).get("usd")) or 0.0
        key = (chain, addr)
        prior = best.get(key)
        # One entry per contract, carrying its deepest pool.
        if prior is None or liq > prior.liquidity_usd:
            best[key] = Candidate(
                chain=chain, address=addr,
                symbol=base.get("symbol"), name=base.get("name"),
                liquidity_usd=liq, price_usd=_f(pair.get("priceUsd")))
    return sorted(best.values(), key=lambda c: c.liquidity_usd, reverse=True)


def parse_pair(payload: Optional[Dict[str, Any]], address: str) -> PriceInfo:
    """Aggregate price/liquidity for the token at *address* across its pools.

    ⚠️ ``priceUsd`` on a DexScreener pair is always the price of that pair's
    **base** token, and ``/tokens/<addr>`` returns pools where the token sits on
    EITHER side. So only base-side pools may supply the price.

    Caught in live verification: ``/tokens/<USDC>`` returns 30 pools, 6 with USDC
    as the quote. Taking the deepest pool regardless of side priced USDC at
    $0.44 — the AERO price from the deepest USDC-quoted pool. In a valuation
    path that understates a USDC holding by more than half, and at T3 it would
    corrupt cap arithmetic.

    A token that appears only as a quote has NO price here — unknown, which is
    always better than another token's number.
    """
    pairs = (payload or {}).get("pairs") or []
    target = (address or "").lower()
    mine = [p for p in pairs
            if ((p.get("baseToken") or {}).get("address") or "").lower() == target]
    if not mine:
        return PriceInfo(price_usd=None, liquidity_usd=None, pool_count=0,
                         confidence="unknown")
    total_liq = 0.0
    deepest = None
    deepest_liq = -1.0
    for pair in mine:
        liq = _f((pair.get("liquidity") or {}).get("usd")) or 0.0
        total_liq += liq
        if liq > deepest_liq:
            deepest_liq, deepest = liq, pair
    price = _f((deepest or {}).get("priceUsd"))
    return PriceInfo(price_usd=price, liquidity_usd=total_liq,
                     pool_count=len(mine),
                     confidence=confidence_for(price, total_liq, len(mine)))


# --------------------------------------------------------------------------
# Network boundary — never exercised by unit tests.
# --------------------------------------------------------------------------

def _get(url: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, headers={"user-agent": "polyrob-defi/1.0"})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        logger.debug("dexscreener: request failed for %s", url, exc_info=True)
        return None


def search(symbol: str, timeout: float = 8.0) -> List[Candidate]:
    return parse_search(_get(f"{SEARCH_URL}?q={symbol}", timeout))


def token(chain: str, address: str, timeout: float = 8.0) -> PriceInfo:
    payload = _get(f"{TOKEN_URL}/{address}", timeout)
    if payload and payload.get("pairs"):
        payload = {"pairs": [p for p in payload["pairs"] if p.get("chainId") == chain]}
    return parse_pair(payload, address)


def health() -> bool:
    return _get(f"{SEARCH_URL}?q=USDC", 5.0) is not None
