"""``defi_data.swap_quote`` — a read-only route quote, cross-checked against the indexer.

The quote cross-check (pure) and the sync body behind ``swap_quote``. Split out
of ``data_tool.py`` (W9, behaviour-preserving); ``data_tool`` re-exports
``quote_cross_check`` and ``QuoteCrossCheck``, and the body is a mixin, so every
``DefiDataTool._*`` name resolves as before.
"""
from __future__ import annotations

from typing import Optional

from tools.defi.data_params import SwapQuoteParams
from tools.defi.data_render import _fmt_usd


# --- quote cross-check (2026-09-19) ------------------------------------------
# A route quote is INPUT to the swap rail's min-out floor. On 2026-09-19 the
# `lifi:fly` route answered ~4.1 WETH for four unrelated small tokens on a cold
# first response, and the EXIT rail burned steps re-quoting "implausible"
# numbers by eye. The keyless indexer price both sides already carry gives an
# implied output; when the route disagrees by more than QUOTE_SUSPECT_RATIO
# either way the quote is SUSPECT. Fail-open: no price, zero price, or a
# pool with no real liquidity (a liquidity:0 pool once priced a token at
# 1e35) ⇒ "unavailable", never a verdict.
QUOTE_SUSPECT_RATIO = 5.0


class QuoteCrossCheck:
    __slots__ = ("verdict", "ratio", "implied_out", "why")

    def __init__(self, verdict: str, ratio: Optional[float], implied_out: Optional[float], why: str):
        self.verdict = verdict          # "consistent" | "suspect" | "unavailable"
        self.ratio = ratio              # quoted / implied
        self.implied_out = implied_out
        self.why = why


def _usable_price(info) -> Optional[float]:
    try:
        usd = float(getattr(info, "price_usd", None) or 0)
        liq = getattr(info, "liquidity_usd", None)
    except (TypeError, ValueError):
        return None
    if usd <= 0 or liq is None or float(liq) <= 0:
        return None
    return usd


def quote_cross_check(amount_in: float, quoted_out: float, price_in, price_out) -> QuoteCrossCheck:
    """Compare a route's output with the indexer-implied output. Pure."""
    p_in = _usable_price(price_in)
    p_out = _usable_price(price_out)
    if p_in is None or p_out is None:
        missing = "token_in" if p_in is None else "token_out"
        return QuoteCrossCheck("unavailable", None, None,
                               f"no indexed price with real liquidity for {missing}")
    implied = amount_in * p_in / p_out
    if implied <= 0 or quoted_out <= 0:
        return QuoteCrossCheck("unavailable", None, implied, "non-positive side")
    ratio = quoted_out / implied
    if ratio > QUOTE_SUSPECT_RATIO or ratio < 1.0 / QUOTE_SUSPECT_RATIO:
        return QuoteCrossCheck("suspect", ratio, implied,
                               f"route output is {ratio:.1f}x the indexer-implied output")
    return QuoteCrossCheck("consistent", ratio, implied,
                           f"within {ratio:.2f}x of the indexer-implied output")


class QuoteReadsMixin:
    """Mixed into :class:`tools.defi.data_tool.DefiDataTool`."""

    def _swap_quote_sync(self, params: SwapQuoteParams, execution_context=None):
        from tools.defi.data_tool import _quote_slippage_bps  # call-time
        from core.wallet.tokens import get_token_identity

        # 'native' is a first-class side here, exactly as it is on the swap verb
        # (which PREFERS it when the wallet holds the gas asset). The EVM route
        # providers price the WRAPPED asset, so the literal resolves to the
        # chain registry's wrapped_native row — before validation, which would
        # otherwise refuse "'native' is not a 20-byte hex address" and cost the
        # money rails one wasted step per quote (13 refusals in 3 days, 09-19..21).
        via_native = []
        token_in, token_out = params.token_in, params.token_out
        if isinstance(token_in, str) and token_in.strip().lower() == "native":
            token_in, why = self._native_as_wrapped(params.chain)
            if token_in is None:
                return self._ar(error=why)
            via_native.append(f"token_in 'native' quoted via wrapped-native {token_in}")
        if isinstance(token_out, str) and token_out.strip().lower() == "native":
            token_out, why = self._native_as_wrapped(params.chain)
            if token_out is None:
                return self._ar(error=why)
            via_native.append(f"token_out 'native' quoted via wrapped-native {token_out}")

        addr_in, err = self._validate(params.chain, token_in)
        if err:
            return self._ar(error=err)
        addr_out, err = self._validate(params.chain, token_out)
        if err:
            return self._ar(error=err)
        if addr_in.lower() == addr_out.lower():
            return self._ar(error="token_in and token_out are the same token")

        id_in = self._identity(params.chain, addr_in)
        id_out = self._identity(params.chain, addr_out)
        if id_in.decimals is None or id_out.decimals is None:
            return self._ar(error=(
                "one side does not report decimals — refusing to size a quote "
                "against a token whose denomination is unknown"))

        # Solana's route_hints are empty BY DESIGN — Jupiter routes it from
        # inside defi_trade.solana_swap, not through the EVM route providers
        # this verb drives. Falling through to the generic "no route" answer
        # would tell an agent that was told to "quote it FIRST" that Solana
        # tokens are unreachable, which is the false belief this whole change
        # set exists to remove. Name the verb that DOES quote there.
        from core.wallet import chains as _chains
        _row = _chains.get(params.chain)
        if _row is not None and _row.family == "svm":
            return self._ar(content=(
                f"swap_quote does not price {params.chain} — this verb drives "
                f"the EVM route providers, and Solana is routed by Jupiter "
                f"from inside the swap verb itself.\n"
                f"  Quote it with: defi_trade.solana_swap(..., dry_run=true)\n"
                f"  That returns the Jupiter route, the simulated output and "
                f"the guard verdict without broadcasting anything. This is NOT "
                f"'no route' and NOT 'unbuyable' — it is the wrong verb."))

        amount_in_raw = int(round(params.amount_in * (10 ** id_in.decimals)))
        route, route_why = self._route(params.chain, addr_in, addr_out,
                                       amount_in_raw, holder=self._quote_holder(),
                                       slippage_bps=_quote_slippage_bps())
        if route is None:
            return self._ar(content=(
                f"cannot route {id_in.symbol or addr_in} -> "
                f"{id_out.symbol or addr_out}: {route_why} "
                f"Either way this is UNKNOWN, not a zero-value trade."))

        out_human = route.amount_out_raw / (10 ** id_out.decimals)
        rate = out_human / params.amount_in if params.amount_in else 0
        # 2026-09-19: a quote is a time-stamped observation. The history
        # compaction pass collapses byte-identical tool outputs into a
        # back-reference, so two re-quotes minutes apart that agreed became
        # "[duplicate of an earlier tool result]" and a fresh read was
        # indistinguishable from a suppressed call. Stamp the read time and
        # name both addresses so no two quotes are identical bytes.
        import time as _time
        from datetime import datetime as _dt, timezone as _tz
        _qt = float(getattr(route, "quoted_at", 0) or 0) or _time.time()
        _stamp = _dt.fromtimestamp(_qt, _tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Cross-check against the indexer-implied output (fail-open). The OUT
        # side's indexed price is read once and also values the output in USD
        # below — the EXIT rail quotes every row into WETH and was converting
        # by a remembered ETH price ("WETH≈$2,700 est", 2026-09-22 02:01Z).
        p_out_usd = None
        try:
            price_out = self._price_for(params.chain, addr_out)
            p_out_usd = _usable_price(price_out)
            xc = quote_cross_check(params.amount_in, out_human,
                                   self._price_for(params.chain, addr_in), price_out)
        except Exception as e:  # noqa: BLE001 — a read-side check never blocks the quote
            xc = QuoteCrossCheck("unavailable", None, None, f"price lookup failed: {e}")
        if p_out_usd is not None:
            value_line = (f"  value:  ≈ {_fmt_usd(out_human * p_out_usd)} "
                          f"(output at the indexed {_fmt_usd(p_out_usd)} per "
                          f"{id_out.symbol or 'out'}; an indexer read, not a fill)\n")
        else:
            value_line = (f"  value:  unknown — no indexed price with real liquidity "
                          f"for {id_out.symbol or addr_out}; do not substitute a "
                          f"remembered rate\n")
        if xc.verdict == "suspect":
            head = (f"  ⚠ SUSPECT QUOTE — {xc.why} (implied "
                    f"{xc.implied_out:.8f} {id_out.symbol or 'out'}, quoted "
                    f"{out_human:.8f}, {xc.ratio:.1f}x). Re-quote before acting and "
                    f"do NOT use this number as a min-out floor.\n")
        elif xc.verdict == "consistent":
            head = f"  cross-check: consistent — {xc.why}\n"
        else:
            head = f"  cross-check: unavailable — {xc.why}\n"
        for note in via_native:
            head += f"  {note}\n"
        return self._ar(content=(
            f"{params.amount_in} {id_in.symbol or addr_in} -> "
            f"{out_human:.8f} {id_out.symbol or addr_out}\n"
            + head + value_line +
            f"  quoted_at: {_stamp}   pair: {addr_in} -> {addr_out}\n"
            f"  route:  {route.venue}\n"
            f"  rate:   {rate:.8f} {id_out.symbol or 'out'} per "
            f"{id_in.symbol or 'in'}\n"
            f"  spender: {route.spender}   (this is what approve_token must "
            f"authorise — NOT the token address)\n"
            f"  NOTE: a quote is what the route says right now, not a promise. "
            f"defi_trade.swap re-quotes and bounds it with slippage before "
            f"executing, and asks this same set of providers."))
