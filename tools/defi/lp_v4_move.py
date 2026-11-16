"""118 W7: move a v4 position to a new range.

A move is an owner DECLARATION (``core.wallet.lp_seed`` with the three
``from_*`` fields) and two guarded transactions, each a shape the guard already
knows: the withdrawal (``lp_remove``) and the mint of the declared range
(``lp_add(seed=…)``). Nothing is swapped: the new range takes what its ratio
allows of the withdrawn amounts and the rest stays in the wallet.

Which step is next is read from the CHAIN, not from a stored flag: the
declaration holds the liquidity the position had (``from_liquidity``). A
position that still holds it has not been withdrawn; one that holds exactly
the remainder has; anything else means the position changed and the move
refuses. So a run that stopped after the withdrawal continues with the mint.
"""
from typing import Tuple

from core.wallet import univ3_math as M
from tools.defi import lp_reads as R, lp_v4 as V


def build_fields(p, rpc, holder, *, ticks, symbol):
    """The declaration of ``lp_add(protocol='v4', move_from=<id>, range=…)``:
    the position's own PoolKey, the pool price now as the anchor, and as the
    two maxima what the withdrawn part holds at that price. ``ticks`` and
    ``symbol`` are the verb tier's own range parser and symbol read, passed in:
    this module imports nothing above the reads."""
    from core.wallet import chains
    if holder is None:
        raise ValueError("the wallet address is not known, so the position's owner cannot be checked")
    pos = V.position(rpc, p.chain, int(p.move_from))
    if pos.owner != str(holder).lower():
        raise ValueError(f"{holder} does not own v4 position #{p.move_from}")
    if not pos.liquidity:
        raise ValueError(f"v4 position #{p.move_from} holds no liquidity to move")
    st = V.pool_state(rpc, p.chain, pos.pool_id)
    if st.sqrt_price_x96 == 0:
        raise ValueError(f"v4 pool {pos.pool_id} is not initialized")
    key = pos.key
    c0, c1 = key["currency0"], key["currency1"]
    dec0 = chains.get(p.chain).native_decimals if c0 == V.ZERO else R.decimals(rpc, p.chain, c0)
    dec1 = R.decimals(rpc, p.chain, c1)
    lo, hi = ticks(p.range, int(key["tickSpacing"]), dec0, dec1)
    if (lo, hi) == (pos.tick_lower, pos.tick_upper):
        raise ValueError(f"position #{p.move_from} is already in ticks [{lo},{hi}]")
    part = pos.liquidity * int(p.move_pct) // 100
    if not part:
        raise ValueError(f"{p.move_pct}% of position #{p.move_from} is no liquidity")
    out0, out1 = M.amounts_for_liquidity(st.sqrt_price_x96, M.sqrt_price_at_tick(pos.tick_lower),
                                         M.sqrt_price_at_tick(pos.tick_upper), part)
    return {"chain": p.chain, "currency0": c0, "currency1": c1, "fee": key["fee"],
            "tick_spacing": key["tickSpacing"], "hooks": key["hooks"], "pool_id": pos.pool_id,
            "tick_lower": lo, "tick_upper": hi, "amount0_raw": out0, "amount1_raw": out1,
            "dec0": dec0, "dec1": dec1, "sym0": symbol(rpc, p.chain, c0),
            "sym1": symbol(rpc, p.chain, c1), "sqrt_price_x96": st.sqrt_price_x96,
            "price_source": "pool",
            "gap_bps": max(1, int(p.slippage_bps)),
            "from_token_id": int(p.move_from), "from_pct": int(p.move_pct),
            "from_liquidity": pos.liquidity}


def stage(f, liquidity_now: int, withdrawal: str = "") -> str:
    """``'withdraw'`` or ``'mint'``, from the position's liquidity now and the
    declaration's own withdrawal mark (``lp_seed.claim_withdrawal``). The mark
    is what ties a smaller position to THIS move: without it the position was
    changed by something else, and with it a full position means the
    withdrawal is pending or reverted — never "send it again"."""
    was = int(f["from_liquidity"])
    part = was * int(f["from_pct"]) // 100
    tid = f["from_token_id"]
    if int(liquidity_now) == was and not withdrawal:
        return "withdraw"
    if int(liquidity_now) == was:
        raise ValueError(
            f"this move already sent its withdrawal ({withdrawal}) and position #{tid} still "
            f"holds all its liquidity: the transaction is pending or it reverted. It is not "
            f"sent twice. Read the transaction; if it reverted, declare the move again")
    if int(liquidity_now) == was - part and withdrawal:
        return "mint"
    raise ValueError(
        f"position #{tid} holds liquidity {liquidity_now}; the declaration was "
        f"made when it held {was}. The position changed, so this move does not run: declare "
        f"it again")


def withdrawn_minimum(f, sqrt_price_x96: int, tick_lower: int, tick_upper: int) -> Tuple[int, int]:
    """The raw ``(currency0, currency1)`` the move's withdrawal returns at least:
    the part's amounts at the pool price, less the declaration's gap."""
    part = int(f["from_liquidity"]) * int(f["from_pct"]) // 100
    a0, a1 = M.amounts_for_liquidity(sqrt_price_x96, M.sqrt_price_at_tick(tick_lower),
                                     M.sqrt_price_at_tick(tick_upper), part)
    keep = 10000 - int(f["gap_bps"])
    return a0 * keep // 10000, a1 * keep // 10000


__all__ = ["build_fields", "stage", "withdrawn_minimum"]
