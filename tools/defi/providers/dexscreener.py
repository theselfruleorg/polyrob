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
from urllib.parse import urlencode
from typing import Any, Dict, List, Optional

from core.wallet.tokens import clean_name, clean_symbol
from tools.defi.providers.base import Candidate, PriceInfo, confidence_for, LIQUIDITY_CONFIDENCE_FLOOR_USD

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens"

name = "dexscreener"


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(value: Any) -> Optional[float]:
    """CR-L04: a USD price is a finite POSITIVE number, otherwise UNPRICED.
    ``priceUsd: "0"`` is an indexer artifact, and a zero price values a holding
    at nothing (or divides by zero) rather than saying "we do not know"."""
    import math
    out = _f(value)
    return out if (out is not None and math.isfinite(out) and out > 0) else None


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
                symbol=clean_symbol(base.get("symbol")),
                name=clean_name(base.get("name")),
                liquidity_usd=liq, price_usd=_price(pair.get("priceUsd")))
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
    deep_pools = set()
    for pair in mine:
        liq = _f((pair.get("liquidity") or {}).get("usd")) or 0.0
        total_liq += liq
        pool = str(pair.get("pairAddress") or "").strip()
        if pool and liq >= LIQUIDITY_CONFIDENCE_FLOOR_USD and _price(pair.get("priceUsd")):
            deep_pools.add(pool)
        if liq > deepest_liq:
            deepest_liq, deepest = liq, pair
    price = _price((deepest or {}).get("priceUsd"))
    # ⚠️ The grade is made on the PRICED pool's depth, not the sum. `price` comes
    # from one pool, so that pool's liquidity is what an attacker must move;
    # depth sitting in pools we are not quoting does not protect this number.
    # `confidence` gates real behaviour — data_tool EXCLUDES a holding's value
    # from a total unless it is "high", and trade_tool refuses to use the price —
    # so grading on the total admitted a thin-pool price into both. Priced ≤ total
    # by construction, so this can only ever demote (2026-09-23).
    priced_liq = max(deepest_liq, 0.0)
    # The venue behind `price`. A blank or missing `pairAddress` stays None —
    # inventing attribution is worse than admitting the provider did not say.
    priced_pool = str((deepest or {}).get("pairAddress") or "").strip() or None
    return PriceInfo(price_usd=price, liquidity_usd=total_liq,
                     pool_count=len(mine),
                     confidence=confidence_for(price, priced_liq, len(deep_pools)),
                     priced_liquidity_usd=priced_liq,
                     priced_pool_address=priced_pool)


# --------------------------------------------------------------------------
# Network boundary — never exercised by unit tests.
# --------------------------------------------------------------------------

def _get(url: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    try:
        from tools.defi.providers import _http
        # The POOLED client — a fresh one pays ~6 s of connection setup on this
        # box (broken outbound IPv6); see the note in _http.
        return _http.get_json(url, timeout=timeout)
    except Exception:
        logger.debug("dexscreener: request failed for %s", url, exc_info=True)
        return None


def search(symbol: str, timeout: float = 8.0) -> List[Candidate]:
    return parse_search(_get(f"{SEARCH_URL}?{urlencode({'q': symbol})}", timeout))


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
    if payload is None:
        # 071 R5: the indexer did not answer — not the same fact as "no pool".
        return PriceInfo(price_usd=None, liquidity_usd=None, pool_count=0,
                         confidence="unknown", error="dexscreener did not answer")
    if payload and payload.get("pairs"):
        payload = {"pairs": [p for p in payload["pairs"]
                             if p.get("chainId") == provider_chain]}
    return parse_pair(payload, address)


#: ``/tokens/v1/{chainId}/{a,b,...}`` — up to 30 addresses per call (measured
#: 2026-10-02). ⚠️ It returns ONE pair per token (not the deepest, not all of
#: them), so it can say whether a token HAS a pool and give an indicative price,
#: but it cannot grade "high" (that needs the pool count). Callers use it to
#: skip the per-token call for tokens with no pool at all.
BATCH_URL = "https://api.dexscreener.com/tokens/v1"
BATCH_MAX = 30


def parse_batch(payload: Any, addresses) -> Dict[str, PriceInfo]:
    """``{address: PriceInfo}`` for every requested token that is the BASE of a
    returned pair (``priceUsd`` is always the base token's price). A token
    absent from the result has no pair on this indexer."""
    wanted = {str(a).lower(): a for a in addresses}
    out: Dict[str, PriceInfo] = {}
    for pair in (payload if isinstance(payload, list) else []):
        if not isinstance(pair, dict):
            continue
        base = str((pair.get("baseToken") or {}).get("address") or "")
        orig = wanted.get(base.lower())
        if orig is None:
            continue
        liq = _f((pair.get("liquidity") or {}).get("usd"))
        price = _price(pair.get("priceUsd"))
        prior = out.get(orig)
        if prior is not None and (prior.liquidity_usd or 0.0) >= (liq or 0.0):
            continue
        out[orig] = PriceInfo(
            price_usd=price, liquidity_usd=liq, pool_count=1,
            confidence=confidence_for(price, liq, 1),
            priced_liquidity_usd=liq,
            priced_pool_address=str(pair.get("pairAddress") or "").strip() or None)
    return out


def tokens_batch(chain: str, addresses, timeout: float = 8.0) -> Optional[Dict[str, PriceInfo]]:
    """Batched presence + indicative price, or ``None`` when ANY chunk failed
    (a partial presence map would read a missing chunk as "no pool")."""
    from core.wallet import chains
    row = chains.get(chain)
    provider_chain = row.dexscreener_id if row else None
    if not provider_chain:
        return None
    addrs = [a for a in dict.fromkeys(addresses) if a]
    out: Dict[str, PriceInfo] = {}
    for i in range(0, len(addrs), BATCH_MAX):
        chunk = addrs[i:i + BATCH_MAX]
        payload = _get(f"{BATCH_URL}/{provider_chain}/{','.join(chunk)}", timeout)
        if payload is None:
            return None
        out.update(parse_batch(payload, chunk))
    return out


def health() -> bool:
    return _get(f"{SEARCH_URL}?q=USDC", 5.0) is not None
