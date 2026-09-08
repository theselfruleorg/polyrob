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


def _our_chain(dexscreener_id: str) -> Optional[str]:
    """Our chain name for a DexScreener chainId, or None if we do not carry it.

    The reverse of the registry's ``dexscreener_id``. Needed because address
    rules are per FAMILY and the payload only names the indexer's own id.
    """
    from core.wallet import chains
    for row in chains.all_rows():
        if row.dexscreener_id == dexscreener_id:
            return row.name
    return None


def _canonical(addr: str, dexscreener_id: str) -> Optional[str]:
    """Canonicalise a provider-supplied address for ITS chain, or None.

    Family-dispatched. This was EIP-55 for everything, which was correct while
    every row was EVM — but the moment a Solana row existed it would have
    dropped every Solana candidate silently, while the search still reported
    "searched: solana". A provider returning a bad address is misbehaving, so
    the candidate is dropped rather than trusted — never silently, so a schema
    change or a hostile response stays diagnosable.
    """
    chain = _our_chain(dexscreener_id)
    if chain is None:
        return None
    from core.wallet.addresses import normalize_for_chain
    try:
        return normalize_for_chain(chain, addr)
    except ValueError as exc:
        logger.debug("dexscreener: dropping malformed address %r on %s (%s)",
                     addr, dexscreener_id, exc)
        return None


def _supported_chains():
    """DexScreener ids for the chains the registry says it indexes.

    DexScreener also indexes non-EVM chains (Solana et al.) whose addresses are
    base58, not 20-byte hex, so the filter is EXPLICIT rather than an accident
    of address validation — "we searched exactly these chains" stays a statement
    the caller can make honestly. A chain with no ``dexscreener_id`` (Robinhood)
    is simply not searched; claiming it would fabricate a price.
    """
    from core.wallet import chains
    return tuple(dict.fromkeys(r.dexscreener_id for r in chains.all_rows()
                               if r.dexscreener_id))


#: Chains this tier can identify (derived from the chain registry).
SUPPORTED_CHAINS = _supported_chains()


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
        addr = _canonical(raw_addr, chain)
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
    """Price for ``(chain, address)``.

    ⚠️ ``/tokens/<addr>`` returns pools on EVERY chain the address appears on
    (Ethereum's USDC comes back with pulsechain pairs), and the same address is
    a different token on a different chain. This filter is what stops a foreign
    pool from pricing our token, so it uses the chain's OWN provider id from the
    registry rather than our internal name — a chain whose ids differ would
    otherwise filter on a string DexScreener never emits.
    """
    from core.wallet import chains
    row = chains.get(chain)
    provider_chain = row.dexscreener_id if row else chain
    if not provider_chain:
        # This provider does not index the chain; no price is the honest answer.
        return PriceInfo(price_usd=None, liquidity_usd=None, pool_count=0,
                         confidence="unknown")
    payload = _get(f"{TOKEN_URL}/{address}", timeout)
    if payload and payload.get("pairs"):
        payload = {"pairs": [p for p in payload["pairs"]
                             if p.get("chainId") == provider_chain]}
    return parse_pair(payload, address)


def health() -> bool:
    return _get(f"{SEARCH_URL}?q=USDC", 5.0) is not None
