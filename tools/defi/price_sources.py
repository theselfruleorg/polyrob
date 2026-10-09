"""The price sources behind ``core.intel.price`` (071 §3.3, R5).

``core`` may not import ``tools``, so the sources register themselves here and
every caller in the tools tier imports its price reading from THIS module:

* :func:`quote`         — the full :class:`core.intel.model.PriceQuote` (display);
* :func:`spend_price`   — a price that may bound a cap (high, not disputed);
* :func:`exit_price`    — the guard's exit-exemption price (028);
* :func:`indexer_price` — the primary pool price at any grade, never disputed;
* :func:`prefetch` + :func:`quote_prefetched` — the batched holdings path.

No call site talks to DexScreener directly any more. The sources:

1. ``dexscreener``  — PRIMARY: the pool source that grades (>= 2 pools AND
   >= $50k in the priced pool = "high"). Looked up as ``dexscreener.token`` at
   call time so a test stub of that function still applies.
2. ``geckoterminal`` — ``/simple/.../token_price`` (batched, 30 per call).
3. ``jupiter``      — Price API v3, Solana only (batched, 50 per call).

All keyless. A DATA source never routes anything.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

from core.intel import price as _intel
from core.intel.model import PriceQuote, SourceAnswer

logger = logging.getLogger(__name__)

DEXSCREENER = "dexscreener"
GECKOTERMINAL = "geckoterminal"
JUPITER = "jupiter"


def _row(chain: str):
    from core.wallet import chains
    return chains.get(chain)


def _covers_ds(chain: str) -> bool:
    row = _row(chain)
    return bool(row is not None and row.dexscreener_id)


def _covers_gt(chain: str) -> bool:
    row = _row(chain)
    return bool(row is not None and row.geckoterminal_id)


def _covers_jup(chain: str) -> bool:
    row = _row(chain)
    return bool(row is not None and row.family == "svm" and row.name == "solana")


def _ds_answer_from(info) -> SourceAnswer:
    err = getattr(info, "error", None)
    return SourceAnswer(
        name=DEXSCREENER, usd=getattr(info, "price_usd", None),
        liquidity_usd=getattr(info, "liquidity_usd", None),
        priced_liquidity_usd=getattr(info, "priced_liquidity_usd", None),
        pool=getattr(info, "priced_pool_address", None),
        pool_count=getattr(info, "pool_count", None),
        confidence=None if err else (getattr(info, "confidence", None) or "unknown"),
        error=err)


def _ds_source(chain: str, address: str) -> SourceAnswer:
    from tools.defi.providers import dexscreener
    return _ds_answer_from(dexscreener.token(chain, address))


def _gt_source(chain: str, address: str) -> SourceAnswer:
    from tools.defi.providers import geckoterminal
    got = geckoterminal.token_prices(chain, [address])
    return SourceAnswer(name=GECKOTERMINAL, usd=got.get(address))


def _jup_source(chain: str, address: str) -> SourceAnswer:
    from tools.defi.providers import jupiter_price
    got = jupiter_price.prices([address])
    return SourceAnswer(name=JUPITER, usd=got.get(address))


def register() -> None:
    """Idempotent. Runs at import; callable again after a test resets the registry."""
    _intel.register_source(DEXSCREENER, _ds_source, covers=_covers_ds, primary=True)
    _intel.register_source(GECKOTERMINAL, _gt_source, covers=_covers_gt)
    _intel.register_source(JUPITER, _jup_source, covers=_covers_jup)


register()


def _ensure() -> None:
    if not _intel.registered_sources():
        register()


# -- the one function every reader goes through ---------------------------

def quote(chain: str, address: str, **kw) -> PriceQuote:
    _ensure()
    return _intel.quote(chain, address, **kw)


def spend_price(chain: str, address: str) -> Optional[float]:
    _ensure()
    return _intel.spend_price(chain, address)


def exit_price(chain: str, address: str) -> Optional[float]:
    _ensure()
    return _intel.exit_price(chain, address)


def trusted_buy_price(chain: str, address: str) -> Optional[float]:
    _ensure()
    return _intel.trusted_buy_price(chain, address)


def indexer_price(chain: str, address: str) -> Optional[float]:
    _ensure()
    return _intel.indexer_price(chain, address)


# -- batched holdings path ------------------------------------------------

@dataclass
class Prefetch:
    """What the batch calls learned before the per-row loop.

    ``ds_batch`` is ``None`` when the DexScreener batch did not answer (then
    every row asks per token, as before). ``secondary`` maps a source name to
    its prices, or to ``None`` when that batch failed (named per row).
    """
    chain: str
    ds_batch: Optional[Dict[str, object]] = None
    secondary: Dict[str, Optional[Dict[str, float]]] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    #: The addresses the batches were asked about. A row outside it is priced
    #: exactly as before (one ``quote``), never read as "no pair".
    covered: frozenset = frozenset()


#: The prefetch asks about at most this many addresses (3 DexScreener / GT
#: calls, 2 Jupiter calls), in the caller's priority order. Measured
#: 2026-10-02 on an exchange wallet with 3,092 mints: batching ALL of them
#: spent the whole 40 s pricing budget before the first row was priced.
PREFETCH_MAX = 90


def prefetch(chain: str, addresses: Iterable[str]) -> Prefetch:
    """One batch call per source per 30 (DS, GT) / 50 (Jupiter) addresses, for
    the first ``PREFETCH_MAX`` addresses (pass them in priority order)."""
    addrs = [a for a in dict.fromkeys(addresses) if a][:PREFETCH_MAX]
    pre = Prefetch(chain=chain, covered=frozenset(addrs))
    if not addrs:
        return pre
    if _covers_ds(chain):
        try:
            from tools.defi.providers import dexscreener
            pre.ds_batch = dexscreener.tokens_batch(chain, addrs)
        except Exception as exc:
            pre.ds_batch = None
            pre.errors[DEXSCREENER] = exc.__class__.__name__
    for name, covers, fetch in (
            (GECKOTERMINAL, _covers_gt, _gt_batch),
            (JUPITER, _covers_jup, _jup_batch)):
        if not covers(chain):
            continue
        try:
            pre.secondary[name] = fetch(chain, addrs)
        except Exception as exc:
            logger.debug("price prefetch: %s failed", name, exc_info=True)
            pre.secondary[name] = None
            pre.errors[name] = exc.__class__.__name__
    return pre


def _gt_batch(chain, addrs):
    from tools.defi.providers import geckoterminal
    return geckoterminal.token_prices(chain, addrs)


def _jup_batch(chain, addrs):
    from tools.defi.providers import jupiter_price
    return jupiter_price.prices(addrs)


def _batch_has(batch: Dict[str, object], address: str) -> bool:
    if address in batch:
        return True
    low = address.lower() if address.startswith("0x") else None
    return low is not None and any(k.lower() == low for k in batch)


def quote_prefetched(chain: str, address: str, pre: Optional[Prefetch], *,
                     must_ask_primary: bool = False) -> PriceQuote:
    """``quote`` with the batch answers handed in.

    The secondary sources answer from the batch (no call). The primary is
    still asked per token — its "high" grade needs every pool — EXCEPT for a
    token the DexScreener batch answered without: that token has no pair on
    the indexer, so the per-token call would only confirm "no pool". A token
    the caller deliberately holds (``must_ask_primary``: canonical, pinned,
    own launch) is always asked, so an indexer quirk can never put it under
    the dust warning.
    """
    if pre is None or address not in pre.covered:
        return quote(chain, address)
    answers: Dict[str, SourceAnswer] = {}
    for name, prices in pre.secondary.items():
        if prices is None:
            answers[name] = SourceAnswer(name=name, error=pre.errors.get(name, "batch failed"))
        else:
            answers[name] = SourceAnswer(name=name, usd=prices.get(address))
    if (pre.ds_batch is not None and not must_ask_primary
            and not _batch_has(pre.ds_batch, address)):
        answers[DEXSCREENER] = SourceAnswer(name=DEXSCREENER, usd=None, pool_count=0,
                                            confidence="unknown")
    return quote(chain, address, answers=answers)
