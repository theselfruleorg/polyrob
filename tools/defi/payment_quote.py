"""The tools-tier price quoter for 046 paid room actions.

Registered as the container service ``payment_quoter``. `core/payments/quote.py`
holds the POLICY (freshness, screen verdict, liquidity floor, token floor); this
holds the READ.

⚠️ Fail-open to ``None``. A missing quote is a REFUSAL upstream
(`size_amount_raw`), never a zero price — so an indexer outage costs a paid
action rather than giving one away.

⚠️ The price and the screen come from the SAME pool, and that pool is the
DEEPEST one. A token's pools disagree, and the thin one is the one an attacker
seeded; pricing against one pool while screening another would let the screen
bless liquidity the price never touched.
"""
from __future__ import annotations

import logging
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
    prices = {}
    for item in ((payload or {}).get("data") or []):
        attrs = (item or {}).get("attributes") or {}
        addr = str(attrs.get("address") or "").strip()
        price = attrs.get("base_token_price_usd")
        if addr and price is not None:
            try:
                prices[addr] = float(price)
            except (TypeError, ValueError):
                continue

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


class PaymentQuoter:
    """One quote per asset, from the DEEPEST pool the indexer knows."""

    def __init__(self, pool_fn: Optional[Callable] = None) -> None:
        #: Injected for tests. ``(chain, address) -> (pool, usd_per_token) | None``
        self._pool_fn = pool_fn or _deepest_priced_pool

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
            from tools.defi.pool_screen import classify
            verdict = classify(pool)
            return PriceQuote(
                asset_id=getattr(asset, "asset_id", ""),
                usd_per_token=float(price),
                liquidity_usd=float(getattr(pool, "liquidity_usd", 0) or 0),
                verdict=verdict.verdict, source="geckoterminal", ts=time.time())
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
