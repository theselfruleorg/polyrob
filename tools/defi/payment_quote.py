"""The tools-tier price quoter for 046 paid room actions.

Registered as the container service ``payment_quoter``. `core/payments/quote.py`
holds the POLICY (freshness, screen verdict, liquidity floor, token floor); this
holds the READ.

⚠️ Fail-open to ``None``. A missing quote is a REFUSAL upstream
(`size_amount_raw`), never a zero price — so an indexer outage costs a paid
action rather than giving one away.

The deepest pool supplies the screen, checked against the shared spend-grade
price seam. A single spot pool cannot set an invoice price by itself.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Optional, Tuple

from core.payments.quote import PriceQuote

logger = logging.getLogger(__name__)


def _deepest_priced_pool(chain: str, address: str, *,
                         fetch: Optional[Callable] = None) -> Optional[Tuple[Any, float]]:
    """``(PoolCandidate, usd_per_token)`` for the deepest indexed pool, or None.

    One payload, two reads: the candidate the screen classifies and the price
    the fee is sized against, so they can never describe different pools.
    """
    from core.wallet import chains
    from tools.defi.providers import geckoterminal as gt

    row = chains.get(chain)
    if row is None or not row.geckoterminal_id:
        return None
    token = gt._url_safe_address(chain, address, what="token")
    url = (f"{gt.BASE_URL}/networks/{row.geckoterminal_id}/tokens/{token}/pools")
    payload = (fetch or gt._get)(url)

    candidates = gt.parse_pools(payload, chain, row.geckoterminal_id)
    # CR-M06: the price must be THIS asset's side of the pool. GeckoTerminal
    # lists a token's pools with the token on EITHER side, and
    # `base_token_price_usd` is the OTHER token's price when ours is the quote
    # side — USDC priced at $3000 off a WETH/USDC pool, and a pool creator
    # choosing which side an invoice is sized against. A pool where the asset
    # is on neither side (or the side cannot be read) is not a price at all.
    from core.wallet.addresses import normalize_for_chain
    try:
        want = normalize_for_chain(chain, str(address).strip())
    except ValueError:
        return None
    prices = {}
    for item in ((payload or {}).get("data") or []):
        attrs = (item or {}).get("attributes") or {}
        rel = (item or {}).get("relationships") or {}
        addr = str(attrs.get("address") or "").strip()
        if not addr:
            continue
        if _side_is(rel, "base_token", row.geckoterminal_id, chain, want):
            price = attrs.get("base_token_price_usd")
        elif _side_is(rel, "quote_token", row.geckoterminal_id, chain, want):
            price = attrs.get("quote_token_price_usd")
        else:
            continue
        if price is None:
            continue
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            prices[addr] = value
    # Only a pool whose price for OUR side is known can be the deepest pool:
    # otherwise a deeper pool we cannot price for this asset would turn a real
    # quote into a refusal, and a pool with the asset on neither side would win.
    candidates = [c for c in candidates if c.pool_address in prices]

    best = None
    for c in candidates:
        if c.liquidity_usd is None:
            continue
        if best is None or c.liquidity_usd > best.liquidity_usd:
            best = c
    if best is None:
        return None
    price = prices.get(best.pool_address)
    if price is None:
        # A pool with no price is not a price. Refusing here is the honest
        # answer; the caller turns it into a named refusal.
        return None
    return best, price


def _side_is(rel, side: str, gt_network: str, chain: str, want: str) -> bool:
    """True when the pool's ``side`` (``base_token``/``quote_token``) is ``want``.

    The relationship id is ``<network>_<address>``; a wrong-network prefix or a
    malformed address is "not this side", never a guess.
    """
    raw = (((rel or {}).get(side) or {}).get("data") or {}).get("id") or ""
    prefix = f"{gt_network}_"
    if not isinstance(raw, str) or not raw.startswith(prefix):
        return False
    from core.wallet.addresses import normalize_for_chain, same_address
    try:
        got = normalize_for_chain(chain, raw[len(prefix):])
    except ValueError:
        return False
    return same_address(got, want)


class PaymentQuoter:
    """Independent spend-grade price plus a corroborating deepest-pool screen."""

    def __init__(self, pool_fn: Optional[Callable] = None,
                 price_fn: Optional[Callable] = None) -> None:
        #: Injected for tests. ``(chain, address) -> (pool, usd_per_token) | None``
        self._pool_fn = pool_fn or _deepest_priced_pool
        from tools.defi.price_sources import spend_price
        self._price_fn = price_fn or spend_price

    def quote(self, asset) -> Optional[PriceQuote]:
        address = getattr(asset, "address", None)
        chain = getattr(asset, "chain", "")
        if not address or not chain:
            return None
        try:
            found = self._pool_fn(chain, address)
        except Exception as e:
            logger.warning("payment quote: pool lookup failed for %s (%s)",
                           getattr(asset, "asset_id", "?"), e)
            return None
        if not found:
            return None
        try:
            pool, price = found
            independent = self._price_fn(chain, address)
            if isinstance(independent, bool) or independent is None:
                return None
            independent = float(independent)
            if not math.isfinite(independent) or independent <= 0:
                return None
            from tools.defi.trade_tool import _route_drift_max_pct
            if (not math.isfinite(float(price))
                    or abs(float(price) - independent) / independent * 100 > _route_drift_max_pct()):
                return None
            from tools.defi.pool_screen import classify
            verdict = classify(pool)
            return PriceQuote(
                asset_id=getattr(asset, "asset_id", ""),
                usd_per_token=independent,
                liquidity_usd=float(getattr(pool, "liquidity_usd", 0) or 0),
                verdict=verdict.verdict, source="independent + geckoterminal screen",
                ts=time.time(), confidence="high")
        except Exception as e:
            logger.warning("payment quote: classify failed for %s (%s)",
                           getattr(asset, "asset_id", "?"), e)
            return None




def register_payment_quoter(container) -> bool:
    """Register the ``payment_quoter`` service on *container*. Idempotent.

    ⚠️ ONE join, called from BOTH container builders (`core/bootstrap.py` for
    the CLI/headless agent, `core/initialization.py` for the server), because
    the two drifting is exactly how this service came to have call sites in
    `core/surfaces/room_actions.py` and `modules/x402/invoicing.py` and no
    registration anywhere — so every non-stable asset was unpriceable.

    Construction does NO I/O (the indexer is read per `quote()` call), so this
    is safe to run unconditionally at startup. Fail-open: a registration error
    leaves the service absent, which downstream reads as a REFUSAL to price,
    never as a free or par-priced action.
    """
    try:
        if container.has_service("payment_quoter"):
            return True
        container.register_service("payment_quoter", PaymentQuoter())
        return True
    except Exception as e:
        logger.debug("payment_quoter not registered: %s", e)
        return False


__all__ = ["PaymentQuoter", "register_payment_quoter"]
