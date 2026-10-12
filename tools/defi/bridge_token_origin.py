"""The Solana half of a pinned-USDC bridge ORIGIN (`defi_trade.bridge`).

Relay's SPL deposit is ONE transaction that calls its pinned program (no
allowance exists on Solana), so the native-origin assertions carry over with
the asset swapped: exactly the declared USDC leaves, no other token leaves, no
authority changes (checked by the caller), and SOL beyond the fee and the rent
of accounts we keep is CHARGED to the value the caps see — never waved through.
The EVM half (an exact approve to the pinned depository, then the guarded
deposit) is `bridge_evm_leg.EvmTokenOriginLeg`.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def svm_token_origin_value(tool, deltas, mint: str, amount_in_raw: int, decimals: int,
                           *, price_sol) -> Tuple[Optional[float], Optional[str], str]:
    """``(usd, refusal, header_note)`` for a simulated SPL-origin bridge.

    ``price_sol(tool, sol) -> usd | None`` is passed in, not imported: this
    module imports nothing from ``tools`` so it stays out of the import cycle.
    """
    token_deltas = deltas.token_deltas or {}
    moved = token_deltas.get(mint)
    if moved is None:
        return None, ("the simulation did not measure the USDC being sent — an "
                      "unmeasured outflow is not a free one"), ""
    if -int(moved) != int(amount_in_raw):
        return None, (f"the simulation moves {-int(moved)} raw USDC, not the declared "
                      f"{amount_in_raw} — that is not the order that was priced"), ""
    others = {m: d for m, d in token_deltas.items() if m != mint and d < 0}
    if others:
        return None, f"undeclared token outflow beside the USDC: {others}", ""
    excess = deltas.native_excess
    if excess is None:
        return None, ("the SOL fee could not be computed, so the SOL leaving cannot "
                      "be bounded"), ""
    try:
        unit = tool._price("solana", mint)
    except Exception:
        unit = None
    if unit is None or float(unit) <= 0:
        return None, "USDC could not be priced, so no cap can bound this bridge", ""
    from core.money.valuation import usd_ceiling
    usd = usd_ceiling(int(amount_in_raw) / (10 ** int(decimals)) * float(unit))
    if excess > 0:
        sol_usd = price_sol(tool, int(excess) / 1_000_000_000)
        if sol_usd is None:
            return None, (f"{excess} lamports leave beyond fee and rent and could not "
                          f"be priced"), ""
        usd += sol_usd
    note = (f"  simulated: {moved} raw USDC, native excess {excess} lamports "
            f"(fee {deltas.fee_lamports})\n")
    return usd, None, note
