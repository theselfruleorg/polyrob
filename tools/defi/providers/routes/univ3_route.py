"""Uniswap V3 as a ``RouteProvider`` — local construction, tried first.

This is an adapter, not a rewrite: ``providers/univ3.py`` still holds the
quoting and the calldata encoding, and its behaviour is unchanged. All this
module does is present that path through the common seam so a second provider
can sit behind it.

It stays FIRST in the chain wherever a chain has a verified deployment, because
it is the strongest trust position we have — the quote comes from the same
contracts that will execute it, the calldata is ours, and the spender is the
router already pinned in ``core.wallet.chains``. A third party is a reach
extension for pools this cannot see, never a replacement for it.
"""
from __future__ import annotations

from typing import Optional

from tools.defi.providers.routes import RouteQuote


class UniV3RouteProvider:
    name = "univ3"

    def supports(self, chain: str) -> bool:
        from core.wallet import chains
        row = chains.get(chain)
        if row is None or "univ3" not in row.route_hints:
            return False
        return bool(row.univ3_router and row.univ3_quoter)

    def quote(self, chain: str, token_in: str, token_out: str,
              amount_in_raw: int, *, holder: str,
              slippage_bps: int) -> Optional[RouteQuote]:
        from tools.defi.providers import univ3
        from tools.defi.providers.routes import is_native
        if is_native(token_in) or is_native(token_out):
            # Uniswap V3 pools hold the WRAPPED asset; `exactInputSingle` takes
            # token ADDRESSES and pulls them by allowance. Routing native here
            # would mean adding a wrap leg to a locally-built call, which is a
            # different transaction shape with its own assertions. Returning
            # None hands the pair to the aggregator, which does support it —
            # and `best_route` reads None as "this provider has no path", never
            # as "unbuyable".
            return None
        q = univ3.best_quote(chain, token_in, token_out, amount_in_raw)
        if q is None:
            return None
        # amount_out_min is the caller's floor and is applied by `best_route`;
        # it must be encoded into the calldata, so it is derived here too and
        # the two must agree. Passing None would let the seam compute a floor
        # the calldata does not enforce.
        amount_out_min = (q.amount_out_raw * (10_000 - slippage_bps)) // 10_000
        if amount_out_min <= 0:
            return None
        data = univ3.build_exact_input_single_data(
            token_in=token_in, token_out=token_out, fee=q.fee_tier,
            recipient=holder, amount_in_raw=amount_in_raw,
            amount_out_min_raw=amount_out_min)
        return RouteQuote(
            chain=chain, token_in=token_in, token_out=token_out,
            amount_in_raw=amount_in_raw, amount_out_raw=q.amount_out_raw,
            amount_out_min_raw=amount_out_min,
            spender=q.router, to=q.router, calldata=data, value_raw=0,
            venue=f"uniswap-v3 fee {q.fee_tier}", quoted_at=q.quoted_at,
            locally_built=True)
