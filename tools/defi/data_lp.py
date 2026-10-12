"""``defi_data`` liquidity-pool reads — positions, pool state, quotes and metrics.

The sync bodies behind ``lp_positions``, ``lp_pool_info``, ``lp_quote`` and
``pool_metrics`` (v3 and the Pons v4 pools) and their RPC and symbol helpers.
Split out of ``data_tool.py`` (W9, behaviour-preserving) as a mixin, so every
``DefiDataTool._*`` name resolves as before. Module-level names of
``data_tool`` are imported at call time.
"""
from __future__ import annotations

from typing import Callable
from tools.defi.data_params import (
    LpPoolInfoParams, LpPositionsParams, LpQuoteParams, PoolMetricsParams)
from tools.defi.data_render import (
    _LP_MAX_RAW, _LP_V4_NOT_YET, _lp_amount_text, _lp_invert_if_flipped,
    _lp_number_text, _lp_read_error_text, _render_lp_position)
from tools.defi.token_screen import quoted


class LpReadsMixin:
    """Mixed into :class:`tools.defi.data_tool.DefiDataTool`."""

    def _lp_rpc_for(self, chain: str) -> Callable:
        """The `rpc(method, params, timeout=8.0)` the LP reads call. Tests
        inject `lp_rpc` at construction; production builds one per chain,
        matching the existing `_eth_call` pattern."""
        if self._lp_rpc is not None:
            return self._lp_rpc
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        return lambda method, params, timeout=8.0: _rpc(
            rpc_url_for_chain(chain), method, params, timeout)

    def _pool_metrics_sync(self, params: PoolMetricsParams, execution_context=None):
        from tools.defi import lp_reads
        from tools.defi.pool_metrics import pool_metrics
        try:
            out = pool_metrics(self._lp_rpc_for(params.chain), params.chain, params.token,
                               expect_pool_id=params.expect_pool_id,
                               depth_pct=tuple(params.depth_pct),
                               mint_native=params.mint_native,
                               hook_fee_bps=params.hook_fee_bps)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))
        return self._ar(content="\n".join(out["lines"]))

    def _lp_v4_read(self, render, chain: str):
        """048 phase 3 (core handoff W6): a v4 read on a Pons PoolKey. The
        pool is derived from the Pons factory record, never from the caller."""
        from tools.defi import lp_reads, lp_v4
        try:
            return self._ar(content="\n".join(render(lp_v4, self._lp_rpc_for(chain))))
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))

    def _lp_symbol(self, chain: str, address: str) -> str:
        """Best-effort token symbol via the existing `_identity` seam — falls
        back to the raw address when the read is unavailable or fails, so a
        symbol lookup never blocks or breaks an LP render."""
        try:
            ident = self._identity(chain, address)
            return quoted(getattr(ident, "symbol", None)) or address
        except Exception:
            return address

    def _lp_positions_sync(self, params: LpPositionsParams, execution_context=None):
        from tools.defi.data_tool import _operator_read_refusal  # call-time
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)
        address = params.address
        if not address:
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return self._ar(error=refusal)
            try:
                from core.wallet.factory import get_agent_wallet
                w = get_agent_wallet()
                address = w.operational_signer().address if w else None
            except Exception:
                address = None
        if not address:
            return self._ar(error=(
                "no address given and this agent's own wallet could not be "
                "resolved (AGENT_WALLET_ENABLED)"))

        from tools.defi import lp_reads

        rpc = self._lp_rpc_for(params.chain)
        try:
            ids = lp_reads.owned_position_ids(rpc, params.chain, address)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))
        if not ids:
            return self._ar(content=(
                f"{address} on {params.chain}: no Uniswap v3 positions. This "
                f"IS an answer from a working read, not a failed one."))

        lines = [f"{address} on {params.chain} — {len(ids)} Uniswap v3 "
                 f"position(s):"]
        try:
            for token_id in ids:
                pv = lp_reads.position(rpc, params.chain, token_id)
                sym0 = self._lp_symbol(params.chain, pv.token0)
                sym1 = self._lp_symbol(params.chain, pv.token1)
                lines.extend(_render_lp_position(pv, sym0=sym0, sym1=sym1))
                from tools.defi.lp_valuation import value_lines
                lines.extend(value_lines(self, params.chain, pv, execution_context))
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))
        return self._ar(content="\n".join(lines))

    def _lp_pool_info_sync(self, params: LpPoolInfoParams, execution_context=None):
        if params.protocol == "v4":
            return self._lp_v4_read(lambda lp_v4, rpc: lp_v4.pool_info_lines(
                rpc, params.chain, params.token_a, params.token_b), params.chain)
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)

        from tools.defi import lp_reads

        rpc = self._lp_rpc_for(params.chain)
        pool = params.pool
        if not pool:
            if not (params.token_a and params.token_b and params.fee):
                return self._ar(error=(
                    "give either pool, OR token_a + token_b + fee to locate "
                    "a Uniswap v3 pool."))
            try:
                pool = lp_reads.pool_address(
                    rpc, params.chain, params.token_a, params.token_b, params.fee)
            except lp_reads.LpReadError as exc:
                return self._ar(error=_lp_read_error_text(exc))
            if pool is None:
                return self._ar(content=(
                    f"no Uniswap v3 pool exists yet for {params.token_a}/"
                    f"{params.token_b} at fee {params.fee} on {params.chain} "
                    f"— this pool does not exist. Use lp_add with "
                    f"initial_price to create and seed one."))

        try:
            ps = lp_reads.pool_state(rpc, params.chain, pool)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))

        sym0 = self._lp_symbol(params.chain, ps.token0)
        sym1 = self._lp_symbol(params.chain, ps.token1)
        fee_pct = ps.fee / 1e4
        inv_price = None if not ps.price0_in_1 else 1.0 / ps.price0_in_1
        lines = [
            f"pool {ps.pool} on {params.chain} — {sym0}/{sym1} fee {fee_pct:g}%",
            f"  tick: {ps.tick} (spacing {ps.tick_spacing})",
            f"  liquidity: {ps.liquidity:,}",
            f"  price: 1 {sym0} = {_lp_number_text(ps.price0_in_1)} {sym1}   "
            f"|   1 {sym1} = {_lp_number_text(inv_price)} {sym0}",
        ]
        return self._ar(content="\n".join(lines))

    def _lp_quote_sync(self, params: LpQuoteParams, execution_context=None):
        if params.protocol == "v4":
            from tools.defi.lp_reads import decimals
            return self._lp_v4_read(lambda lp_v4, rpc: lp_v4.quote_lines(
                rpc, params.chain, params.token_a, params.token_b, params.amount_a,
                params.amount_b, params.range,
                lambda t: decimals(rpc, params.chain, t)), params.chain)
        if params.protocol != "v3":
            return self._ar(error=_LP_V4_NOT_YET)

        from tools.defi import lp_abi as A
        from tools.defi import lp_reads
        from core.wallet import univ3_math as M

        if params.fee not in A.FEE_TIERS:
            return self._ar(error=(
                f"fee {params.fee} is not a Uniswap v3 tier — use one of "
                f"{', '.join(str(f) for f in sorted(A.FEE_TIERS))}."))
        if params.amount_a is None and params.amount_b is None:
            return self._ar(error=(
                "give amount_a and/or amount_b to quote a deposit."))

        rpc = self._lp_rpc_for(params.chain)
        token0, token1, flipped = M.sort_tokens(params.token_a, params.token_b)

        try:
            pool = lp_reads.pool_address(
                rpc, params.chain, params.token_a, params.token_b, params.fee)
        except lp_reads.LpReadError as exc:
            return self._ar(error=_lp_read_error_text(exc))

        if pool is not None:
            try:
                ps = lp_reads.pool_state(rpc, params.chain, pool)
            except lp_reads.LpReadError as exc:
                return self._ar(error=_lp_read_error_text(exc))
            sqrt_p, dec0, dec1 = ps.sqrt_price_x96, ps.dec0, ps.dec1
            pool_note = f"pool {pool}"
        else:
            if params.initial_price is None:
                return self._ar(error=(
                    f"no Uniswap v3 pool exists yet for {params.token_a}/"
                    f"{params.token_b} at fee {params.fee} on {params.chain} "
                    f"— initial_price (token_b per token_a) is REQUIRED to "
                    f"quote a deposit into a pool that has not been created."))
            if params.initial_price <= 0:
                return self._ar(error="initial_price must be greater than 0.")
            try:
                dec0 = lp_reads.decimals(rpc, params.chain, token0)
                dec1 = lp_reads.decimals(rpc, params.chain, token1)
            except lp_reads.LpReadError as exc:
                return self._ar(error=_lp_read_error_text(exc))
            price0_in_1 = _lp_invert_if_flipped(params.initial_price, flipped)
            sqrt_p = M.sqrt_price_from_price(price0_in_1, dec0, dec1)
            pool_note = "not-yet-created pool"

        spacing = A.FEE_TIERS[params.fee]
        if params.range == "full":
            tick_lower, tick_upper = M.full_range_ticks(spacing)
        else:
            try:
                lo_s, hi_s = params.range.split(",", 1)
                lo, hi = float(lo_s), float(hi_s)
            except (ValueError, AttributeError):
                return self._ar(error=(
                    f"range {params.range!r} is not 'full' or "
                    f"'price_lo,price_hi'."))
            if lo <= 0 or hi <= 0:
                return self._ar(error="range prices must be greater than 0.")
            if flipped:
                price0_lo = _lp_invert_if_flipped(hi, True)
                price0_hi = _lp_invert_if_flipped(lo, True)
            else:
                price0_lo, price0_hi = lo, hi
            tick_lower = M.align_tick(M.price_to_tick(price0_lo, dec0, dec1), spacing)
            tick_upper = M.align_tick(M.price_to_tick(price0_hi, dec0, dec1), spacing)

        if tick_lower >= tick_upper:
            return self._ar(error=(
                f"range collapses to an empty tick window "
                f"[{tick_lower},{tick_upper}] after aligning to the "
                f"{spacing}-tick spacing for fee {params.fee} — widen it."))

        sqrt_a = M.sqrt_price_at_tick(tick_lower)
        sqrt_b = M.sqrt_price_at_tick(tick_upper)

        amount0_human = params.amount_b if flipped else params.amount_a
        amount1_human = params.amount_a if flipped else params.amount_b
        raw0 = (_LP_MAX_RAW if amount0_human is None
                else int(round(amount0_human * (10 ** dec0))))
        raw1 = (_LP_MAX_RAW if amount1_human is None
                else int(round(amount1_human * (10 ** dec1))))

        liquidity = M.liquidity_for_amounts(sqrt_p, sqrt_a, sqrt_b, raw0, raw1)
        out0, out1 = M.amounts_for_liquidity(sqrt_p, sqrt_a, sqrt_b, liquidity)

        sym_a = self._lp_symbol(params.chain, params.token_a)
        sym_b = self._lp_symbol(params.chain, params.token_b)
        sym0 = self._lp_symbol(params.chain, token0)
        sym1 = self._lp_symbol(params.chain, token1)
        price0_in_1_now = (sqrt_p / M.Q96) ** 2 * (10 ** dec0) / (10 ** dec1)
        implied = _lp_invert_if_flipped(price0_in_1_now, flipped)

        lines = [
            f"lp_quote {params.chain} v3 fee {params.fee / 1e4:g}% — {pool_note}",
            f"  ticks: [{tick_lower},{tick_upper}]",
            f"  liquidity: {liquidity:,}",
            f"  amount0: {_lp_amount_text(out0, dec0)} {sym0}",
            f"  amount1: {_lp_amount_text(out1, dec1)} {sym1}",
            f"  implied price: 1 {sym_a} = {_lp_number_text(implied)} {sym_b}",
        ]
        return self._ar(content="\n".join(lines))
