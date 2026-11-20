"""v4 liquidity on a Pons PoolKey: the lp_add plan, the LP program caps, and the
one Permit2 grant (048 phase 3; core handoff W6; 090 R2/R4). And the lp_collect
plan of any v4 position the wallet or one of its NFT accounts owns (115 W4).

How the ERC-20 leg moves (v4 PositionManager settles ERC-20 only through
Permit2; a signature permit is refused in this tree, so the grant is a
transaction):

1. ``approve_token(token, spender=Permit2, amount)`` — a standing ERC-20
   allowance to the pinned Permit2, a route spender (``tx_guard.pinned_route_spenders``):
   only a guarded Permit2 grant spends it. A confirmed declaration makes both grants.
2. ``approve_token(token, spender=<v4 PositionManager>, amount, via='permit2')``
   — an EXACT Permit2 grant that expires in ``PERMIT2_GRANT_TTL_S`` (<= the
   guard's ``PERMIT2_GRANT_MAX_TTL_S``). Priced by the guard like any grant.
3. ``lp_add(protocol='v4', …)`` — MINT_POSITION + SETTLE_PAIR + SWEEP on the
   PoolKey the Pons factory record names, judged by ``tx_guard``.

The caps (090 R4) are refusals in the verb, before the guard; they never allow
anything the guard refuses. ``LP_ETH_CAP`` defaults to 0: no v4 native deposit
until the owner sets a program cap.
"""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Optional, Tuple

from core.wallet import chains, dex_registry, tx_guard
from tools.defi import lp_abi as A, lp_reads as R, lp_v4 as V

PERMIT2_GRANT_TTL_S = 900
DEADLINE_S = 120          # 090 R2.2: deadline <= 2 min


def lp_caps():
    """``(program_cap_wei, daily_cap_wei, floor_wei)`` from config (090 R4)."""
    from core.env import float_env
    to_wei = lambda eth: int(Decimal(str(max(0.0, eth))) * 10 ** 18)
    return (to_wei(float_env("LP_ETH_CAP", 0.0)), to_wei(float_env("LP_ETH_DAILY_CAP", 2.0)),
            to_wei(float_env("LP_ETH_FLOOR", 0.5)))


def signed_maxima(desired, used, slippage_bps: int) -> list:
    """The maximum each leg may pay: what the liquidity needs at the pool price
    plus ``slippage_bps``, never above the amount asked for, and zero on a leg
    that pays nothing. The largest slippage (1000) stays inside the guard's
    ``MINT_MAX_GAP_BPS``; ``u / (1 - s)`` did not (it refused from 991 up)."""
    return [min(d, u * (10000 + int(slippage_bps)) // 10000 + 1) if u else 0
            for d, u in zip(desired, used)]


def caps_refusal(gate, native_wei: int, balance_wei: int, now: Optional[float] = None,
                 returning: Optional[Tuple[str, int]] = None) -> Optional[str]:
    """Why a v4 add of ``native_wei`` would break an LP program cap, or None.
    The program total is the net of every booked ``lp_add`` (``native_raw``) and
    ``lp_remove`` (``native_back_raw``) in the durable ledger; the daily bound
    sums the adds only. An unreadable ledger refuses (unknown is not zero).
    ``returning`` is ``(asset, wei)``: a withdrawal that WILL land before this
    add (the first half of a move), netted by the same rule."""
    if not native_wei:
        return None
    program, daily, floor = lp_caps()
    if program <= 0:
        return ("LP_ETH_CAP is 0: no v4 native deposit is allowed until the owner sets "
                "the LP program cap (090 R4)")
    sync = getattr(gate, "_sync_shared_ledger", None)
    if sync is not None and not sync():
        return "the spend ledger is unreadable, so the LP caps cannot be summed"
    now = time.time() if now is None else now
    # 098 review (TS-23): a reverted LP add moved nothing — out of the LP caps,
    # as it is out of the rolling USD caps.
    voided_fn = getattr(gate, "_voided", None)
    voided = voided_fn() if callable(voided_fn) else set()
    live = [e for e in gate.audit_log if not (voided and e.get("result_ref") in voided)]
    rows = [e for e in live if e.get("action") == "lp_add" and "native_raw" in e]
    # 118 W1: the program cap is the NET deposit, PER POSITION. A withdrawal with
    # a verified receipt (its row names the position) returns the native it
    # declared as its minimum, in time order, up to what the ledger deposited
    # into THAT position: a position the ledger did not deposit returns none,
    # and so does a withdrawal whose receipt is not read yet. An add whose
    # position is not known stays counted (over-count, never under-count).
    by_position: dict = {}
    for e in sorted(live, key=lambda e: float(e.get("ts") or 0)):
        asset = e.get("asset") or None
        if e.get("action") == "lp_add" and "native_raw" in e:
            by_position[asset] = by_position.get(asset, 0) + int(e["native_raw"])
        elif (e.get("action") == "lp_remove" and "native_back_raw" in e and asset
              and e.get("receipt_state") != "pending_receipt"):
            by_position[asset] = max(0, by_position.get(asset, 0) - int(e["native_back_raw"]))
    if returning is not None and returning[0]:
        asset = str(returning[0])
        by_position[asset] = max(0, by_position.get(asset, 0) - int(returning[1]))
    total = sum(by_position.values())
    day = sum(int(e["native_raw"]) for e in rows if float(e.get("ts") or 0) >= now - 86400)
    eth = lambda w: f"{w / 1e18:.6g} ETH"
    if total + native_wei > program:
        return f"LP_ETH_CAP {eth(program)} would be exceeded ({eth(total)} net deposited + {eth(native_wei)})"
    if daily > 0 and day + native_wei > daily:
        return f"LP_ETH_DAILY_CAP {eth(daily)} would be exceeded (24h {eth(day)} + {eth(native_wei)})"
    if balance_wei - native_wei < floor:
        return (f"LP_ETH_FLOOR: the wallet would keep {eth(balance_wei - native_wei)}, below "
                f"the {eth(floor)} floor")
    return None


def legs_ready(chain, rpc, holder, npm, legs, now, credit=None, notes=None):
    """``(outflows, held)`` for the legs of a v4 mint, each ``(currency, raw
    maximum, decimals)``. An ERC-20 leg needs the standing allowance to Permit2
    and the exact, unexpired Permit2 grant to the PositionManager; every leg
    needs the balance. A refusal names the exact approval to make. A leg of 0
    (a one-sided range) is not an outflow. ``credit`` is ``{currency: raw}`` the
    wallet WILL hold before the send (a move's withdrawal): it counts for the
    balance check only, so the allowances are still judged. ``notes`` (a list) is
    given only for the plan of a declaration, whose live run makes its own Permit2
    grants first (``lp_v4_seed.make_grants``): a missing grant is then a note in that
    list, not a refusal."""
    row = dex_registry.row_for(chain, "v4")
    out, held = [], []
    for cur, n, dec in legs:
        if not n:
            continue
        if cur == V.ZERO:
            balance = int(rpc("eth_getBalance", [holder, "latest"]), 16)
        else:
            to_permit2 = R.allowance(rpc, chain, cur, holder, row.permit2)
            granted, expiration = V.permit2_allowance(rpc, chain, holder, cur, npm)
            if notes is not None and (to_permit2 < n or granted < n
                                      or expiration <= now + DEADLINE_S):
                notes.append(f"the run first makes the Permit2 grant(s) for {cur} (exact, "
                             f"expiring; the guard judges each)")
            elif to_permit2 < n:
                raise ValueError(
                    f"Permit2 holds an ERC-20 allowance of {to_permit2} on {cur}, need {n}. Run "
                    f"defi_trade.approve_token(chain={chain}, token={cur}, "
                    f"spender={row.permit2}, amount={Decimal(n) / 10 ** dec}) first (the pinned "
                    f"Permit2 is a route spender; a live run of a CONFIRMED pool declaration "
                    f"makes it itself).")
            elif granted < n or expiration <= now + DEADLINE_S:
                raise ValueError(
                    f"Permit2 grant to the v4 PositionManager is {granted} (expires {expiration}), "
                    f"need {n}. Run defi_trade.approve_token(chain={chain}, token={cur}, "
                    f"spender={npm}, amount={Decimal(n) / 10 ** dec}, via='permit2') first (a live run "
                    f"of a CONFIRMED pool declaration makes it itself).")
            balance = int(R.view(rpc, cur, A.NPM_BALANCE_OF, [holder]))
        balance += int((credit or {}).get(cur, 0))
        if balance < n:
            raise ValueError(f"insufficient balance of {cur}: {balance} < {n}")
        out.append((None if cur == V.ZERO else cur, n))
        held.append((None if cur == V.ZERO else cur, balance))
    return out, held


def prepare_add_v4(p, rpc, holder, npm, price_fn=None):
    """The v4 lp_add plan. Refuses anything but a full-range new position on
    an initialized Pons PoolKey with a pinned hook."""
    from tools.defi.lp_verbs import Plan, pool_price_check, raw_amount
    if p.range != "full":
        raise ValueError("v4 deposits are full range only (090 R2.1)")
    if p.initial_price is not None:
        raise ValueError("v4 never creates a pool; the Pons PoolKey must already exist")
    if p.token_id is not None:
        raise ValueError("v4 builds a new-position mint only; each tranche is its own position")
    key, pid, flipped = V.resolve_pons_pair(rpc, p.token_a, p.token_b)
    dex_registry.verify_hook(rpc, p.chain, key["hooks"])
    st = V.pool_state(rpc, p.chain, pid)
    if st.sqrt_price_x96 == 0:
        raise ValueError(f"v4 pool {pid} is not initialized")
    c0, c1 = key["currency0"], key["currency1"]
    native0 = c0 == V.ZERO
    dec0 = chains.get(p.chain).native_decimals if native0 else int(R.view(rpc, c0, A.ERC20_DECIMALS))
    dec1 = int(R.view(rpc, c1, A.ERC20_DECIMALS))
    h0, h1 = (p.amount_b, p.amount_a) if flipped else (p.amount_a, p.amount_b)
    desired = [raw_amount(h0, dec0), raw_amount(h1, dec1)]
    if not all(desired):
        raise ValueError("a full-range v4 deposit needs both amounts")
    # Liquidity from the slippage-reduced amounts; the maxima are the desired
    # amounts, so a price move within slippage_bps still settles.
    keep = 10000 - p.slippage_bps
    lo, hi, liq, used0, used1 = V.full_range_quote(
        st.sqrt_price_x96, key["tickSpacing"], desired[0] * keep // 10000, desired[1] * keep // 10000)
    if not 0 < liq <= A.MAX_UINT128 or not (used0 and used1):
        raise ValueError("deposit produces zero or overflowing liquidity")
    maxima = signed_maxima(desired, (used0, used1), p.slippage_bps)
    price_token0 = chains.get(p.chain).wrapped_native if native0 else c0
    note = pool_price_check(p, price_token0, c1, dec0, dec1, st.sqrt_price_x96, price_fn)
    row = dex_registry.row_for(p.chain, "v4")
    now = int(time.time())
    out, held = legs_ready(p.chain, rpc, holder, npm,
                           ((c0, maxima[0], dec0), (c1, maxima[1], dec1)), now)
    unlock = V.encode_mint_unlock(key, lo, hi, liq, maxima[0], maxima[1], holder)
    data = V.encode_modify_liquidities(unlock, now + DEADLINE_S)
    value = maxima[0] if native0 else 0
    intent = tx_guard.TxIntent(
        chain=p.chain, token=None, to=npm, amount_raw=0, max_spend_usd=p.max_spend_usd,
        is_liquidity_op=True, lp_outflows=tuple(out), lp_held_balances=tuple(held),
        watch_spenders=(row.permit2,), lp_position=(npm, None), lp_position_effect="mint",
        expected_events=((row.pool_manager, V.TOPIC_MODIFY_LIQUIDITY),),
        idempotency_key=None)
    return Plan(intent, data, value, pid, (c0, c1), (dec0, dec1),
                f"v4 pool: {pid} (Pons PoolKey, hooks {key['hooks']}); ticks [{lo}, {hi}] full range\n"
                f"liquidity: {liq}; outflows (raw maxima): {out}\n"
                "LP fee 0: the hook takes the pool fee; this position earns none\n" + note)


def prepare_increase_v4(p, rpc, holder, npm, price_fn=None):
    """118 W6: ``lp_add(protocol='v4', token_id=…)`` — more liquidity into a v4
    position the wallet holds, in the position's own range. The PoolKey and the
    ticks are read from the pinned PositionManager; token_a/token_b only say
    which amount is which currency."""
    from core.wallet import univ3_math as M
    from tools.defi.lp_verbs import Plan, pool_price_check, raw_amount
    if p.initial_price is not None or getattr(p, "tick_spacing", None) is not None:
        raise ValueError("an increase takes the position's own pool and range; pass only "
                         "token_id, the two tokens and the two amounts")
    pos = V.position(rpc, p.chain, p.token_id)
    if pos.owner != holder.lower():
        raise ValueError(f"{holder} does not own v4 position #{p.token_id}")
    key = pos.key
    if not dex_registry.hook_pinned(p.chain, key["hooks"]):
        raise ValueError(f"v4 hook {key['hooks']} has no pinned code hash; it runs inside the increase")
    dex_registry.verify_hook(rpc, p.chain, key["hooks"])
    c0, c1 = key["currency0"].lower(), key["currency1"].lower()
    norm = lambda t: V.ZERO if V._is_native(t) else str(t or "").lower()
    a, b = norm(p.token_a), norm(p.token_b)
    if {a, b} != {c0, c1}:
        raise ValueError(f"position #{p.token_id} is on {c0}/{c1}; token_a and token_b must "
                         f"name those two ('native' for the zero address)")
    st = V.pool_state(rpc, p.chain, pos.pool_id)
    if st.sqrt_price_x96 == 0:
        raise ValueError(f"v4 pool {pos.pool_id} is not initialized")
    native0 = c0 == V.ZERO
    dec0 = chains.get(p.chain).native_decimals if native0 else R.decimals(rpc, p.chain, c0)
    dec1 = R.decimals(rpc, p.chain, c1)
    h0, h1 = (p.amount_a, p.amount_b) if a == c0 else (p.amount_b, p.amount_a)
    desired = [raw_amount(h0, dec0), raw_amount(h1, dec1)]
    sa, sb = M.sqrt_price_at_tick(pos.tick_lower), M.sqrt_price_at_tick(pos.tick_upper)
    keep = 10000 - p.slippage_bps
    liq = M.liquidity_for_amounts(st.sqrt_price_x96, sa, sb,
                                  desired[0] * keep // 10000, desired[1] * keep // 10000)
    if not 0 < liq <= A.MAX_UINT128:
        raise ValueError("at the pool price and this position's range the amounts make no "
                         "liquidity (a range above the price takes only currency0, below it "
                         "only currency1)")
    used = M.amounts_for_liquidity(st.sqrt_price_x96, sa, sb, liq)
    maxima = signed_maxima(desired, used, p.slippage_bps)
    # An increase credits the position's fees owed against what it settles, and
    # SETTLE_PAIR reverts on a currency the wallet is then OWED. So the fees
    # must stay well below each leg (and be zero on a leg that pays nothing).
    try:
        fees = V.position_fees(rpc, p.chain, pos)
    except R.LpReadError as exc:
        raise ValueError(f"the fees owed to position #{p.token_id} were NOT READ ({exc}); an "
                         f"increase is not built on an unknown fee balance")
    if any(fee and fee * 2 > u for fee, u in zip(fees, used)):
        raise ValueError(
            f"position #{p.token_id} is owed fees (raw {list(fees)}) that are not small against "
            f"this increase (raw {list(used)}); the increase would revert. Collect first: "
            f"lp_collect(chain='{p.chain}', protocol='v4', token_id={p.token_id}), then add")
    price_token0 = chains.get(p.chain).wrapped_native if native0 else c0
    note = pool_price_check(p, price_token0, c1, dec0, dec1, st.sqrt_price_x96, price_fn)
    row = dex_registry.row_for(p.chain, "v4")
    now = int(time.time())
    out, held = legs_ready(p.chain, rpc, holder, npm,
                           ((c0, maxima[0], dec0), (c1, maxima[1], dec1)), now)
    unlock = V.encode_increase_unlock(p.token_id, liq, maxima[0], maxima[1], c0, c1, holder)
    data = V.encode_modify_liquidities(unlock, now + DEADLINE_S)
    value = maxima[0] if native0 else 0
    intent = tx_guard.TxIntent(
        chain=p.chain, token=None, to=npm, amount_raw=0, max_spend_usd=p.max_spend_usd,
        is_liquidity_op=True, lp_outflows=tuple(out), lp_held_balances=tuple(held),
        watch_spenders=(row.permit2,), lp_position=(npm, p.token_id), lp_position_effect="hold",
        expected_events=((row.pool_manager, V.TOPIC_MODIFY_LIQUIDITY),), idempotency_key=None)
    return Plan(intent, data, value, pos.pool_id, (c0, c1), (dec0, dec1),
                f"v4 pool: {pos.pool_id} (fee {key['fee']}, hooks {key['hooks']}); position: "
                f"{p.token_id}, ticks [{pos.tick_lower}, {pos.tick_upper}]\n"
                f"increase: liquidity {liq} on top of {pos.liquidity}; outflows (raw maxima): "
                f"{out}\n" + note)


def prepare_collect_v4(p, rpc, holder, npm):
    """The v4 lp_collect plan (115 W4): DECREASE_LIQUIDITY(token_id, 0) +
    TAKE_PAIR(currency0, currency1 → holder). *holder* is the address that owns
    the position and receives the fees: the NFT's account when the verb acts
    through one, the wallet otherwise. The PoolKey is read from the pinned
    PositionManager; no approval and no Permit2 (nothing is settled in).

    The declared receipt minimum is 0 for each currency: no fees-owed read
    exists for v4 here, so the guard measures that nothing LEAVES the holder
    and no NFT moves, and the result shows what arrived."""
    from tools.defi.lp_verbs import Plan
    if str(R.view(rpc, npm, A.NPM_OWNER_OF, [p.token_id])).lower() != holder.lower():
        raise ValueError(f"{holder} does not own v4 position #{p.token_id}")
    key = V.position_pool_key(rpc, p.chain, p.token_id)
    if not dex_registry.hook_pinned(p.chain, key["hooks"]):
        raise ValueError(f"v4 hook {key['hooks']} has no pinned code hash; it runs inside the collect")
    dex_registry.verify_hook(rpc, p.chain, key["hooks"])
    c0, c1 = key["currency0"], key["currency1"]
    native0 = c0 == V.ZERO
    dec0 = chains.get(p.chain).native_decimals if native0 else int(R.view(rpc, c0, A.ERC20_DECIMALS))
    dec1 = int(R.view(rpc, c1, A.ERC20_DECIMALS))
    unlock = V.encode_collect_unlock(p.token_id, c0, c1, holder)
    data = V.encode_modify_liquidities(unlock, int(time.time()) + DEADLINE_S)
    inflows = tuple((None if cur == V.ZERO else cur, 0) for cur in (c0, c1))
    intent = tx_guard.TxIntent(
        chain=p.chain, token=None, to=npm, amount_raw=0, max_spend_usd=p.max_spend_usd,
        is_liquidity_op=True, lp_inflows=inflows, lp_position=(npm, p.token_id),
        lp_position_effect="hold", idempotency_key=None)
    from tools.defi.lp_reads import pool_id
    pid = pool_id(key)
    try:
        f0, f1 = V.position_fees(rpc, p.chain, V.position(rpc, p.chain, p.token_id))
        owed = (f"fees owed now (read, not a minimum): {Decimal(f0) / 10 ** dec0:f} + "
                f"{Decimal(f1) / 10 ** dec1:f}\n")
    except Exception as exc:
        owed = f"fees owed now: NOT READ ({type(exc).__name__}); the receipt shows what arrived\n"
    return Plan(intent, data, 0, pid, (c0, c1), (dec0, dec1),
                f"v4 pool: {pid} (fee {key['fee']}, hooks {key['hooks']}); position: {p.token_id}\n"
                f"collect: the fees owed in {c0} and {c1}, paid to {holder}; liquidity unchanged\n"
                + owed)


#: Audit 2026-11-21 L-11: a MOVE's withdrawal floor, ``(token_id, (min0, min1))`` — what the
#: moved part held at the declaration's ANCHOR, less its gap (:func:`anchor_floor`). Set by
#: ``lp_verbs._perform_move`` around its ``lp_remove`` only (:func:`move_floor`).
_MOVE_FLOOR: ContextVar = ContextVar("lp_v4_move_floor", default=None)


def anchor_floor(f) -> Tuple[int, int]:
    """The raw amounts a move's withdrawal must return at least: the declaration's own
    amounts (the moved part at the anchor price, ``lp_v4_move.build_fields``) less its gap."""
    keep = 10000 - int(f["gap_bps"])
    return int(f["amount0_raw"]) * keep // 10000, int(f["amount1_raw"]) * keep // 10000


@contextmanager
def move_floor(token_id: int, floor: Tuple[int, int]):
    tok = _MOVE_FLOOR.set((int(token_id), tuple(int(x) for x in floor)))
    try:
        yield
    finally:
        _MOVE_FLOOR.reset(tok)


def prepare_remove_v4(p, rpc, holder, npm, price_fn=None):
    """The v4 lp_remove plan (117 W7): DECREASE_LIQUIDITY(token_id, part, min0,
    min1) + TAKE_PAIR(currency0, currency1 → holder). The position NFT stays
    (v4 builds no burn). The wallet must own the position; the PoolKey, the
    ticks and the liquidity are read from the pinned PositionManager, and the
    two minima are the amounts at the pool price less ``slippage_bps``. The
    pool price is checked against an independent one (CR-M12) before a live
    withdrawal, as on v3."""
    from tools.defi.lp_verbs import Plan, pool_price_check
    if getattr(p, "burn", False):
        raise ValueError("v4 builds no burn; withdraw with burn=false (the emptied position stays)")
    pos = V.position(rpc, p.chain, p.token_id)
    if pos.owner != holder.lower():
        raise ValueError(f"{holder} does not own v4 position #{p.token_id}")
    key = pos.key
    if not dex_registry.hook_pinned(p.chain, key["hooks"]):
        raise ValueError(f"v4 hook {key['hooks']} has no pinned code hash; it runs inside the withdrawal")
    dex_registry.verify_hook(rpc, p.chain, key["hooks"])
    liq = pos.liquidity * p.liquidity_pct // 100
    if not liq:
        raise ValueError("position has no removable liquidity; use lp_collect for the fees owed")
    st = V.pool_state(rpc, p.chain, pos.pool_id)
    if st.sqrt_price_x96 == 0:
        raise ValueError(f"v4 pool {pos.pool_id} is not initialized")
    c0, c1 = key["currency0"], key["currency1"]
    native0 = c0 == V.ZERO
    dec0 = chains.get(p.chain).native_decimals if native0 else int(R.view(rpc, c0, A.ERC20_DECIMALS))
    dec1 = int(R.view(rpc, c1, A.ERC20_DECIMALS))
    from core.wallet import univ3_math as M
    amounts = M.amounts_for_liquidity(st.sqrt_price_x96, M.sqrt_price_at_tick(pos.tick_lower),
                                      M.sqrt_price_at_tick(pos.tick_upper), liq)
    minimums = [max(1, n * (10000 - p.slippage_bps) // 10000) if n else 0 for n in amounts]
    floor = _MOVE_FLOOR.get()
    if floor is not None and floor[0] == int(p.token_id):
        # L-11: a move's minimums are bound to the declaration ANCHOR, not only to spot. The
        # "independent" price of a token with one pool is the indexer's read of that same
        # pool, so spot and the check can move together over several blocks.
        short = [i for i in (0, 1) if floor[1][i] > amounts[i]]
        if short:
            raise ValueError(
                f"at the pool price now the withdrawal returns {amounts[0]}/{amounts[1]} raw, "
                f"less than the move's floor {floor[1][0]}/{floor[1][1]} (the declaration's "
                f"amounts at its anchor, less the gap): the pool moved. Declare the move again")
        minimums = [max(m, a) for m, a in zip(minimums, floor[1])]
    if not any(minimums):
        raise ValueError("the withdrawal would return nothing at the pool price")
    price_token0 = chains.get(p.chain).wrapped_native if native0 else c0
    note = pool_price_check(p, price_token0, c1, dec0, dec1, st.sqrt_price_x96, price_fn)
    unlock = V.encode_decrease_unlock(p.token_id, liq, minimums[0], minimums[1], c0, c1, holder)
    data = V.encode_modify_liquidities(unlock, int(time.time()) + DEADLINE_S)
    row = dex_registry.row_for(p.chain, "v4")
    inflows = tuple((None if cur == V.ZERO else cur, m) for cur, m in zip((c0, c1), minimums))
    intent = tx_guard.TxIntent(
        chain=p.chain, token=None, to=npm, amount_raw=0, max_spend_usd=p.max_spend_usd,
        is_liquidity_op=True, lp_inflows=inflows, lp_position=(npm, p.token_id),
        lp_position_effect="hold",
        expected_events=((row.pool_manager, V.TOPIC_MODIFY_LIQUIDITY),), idempotency_key=None)
    return Plan(intent, data, 0, pos.pool_id, (c0, c1), (dec0, dec1),
                f"v4 pool: {pos.pool_id} (fee {key['fee']}, hooks {key['hooks']}); position: {p.token_id}\n"
                f"withdraw: {p.liquidity_pct}% = liquidity {liq} of {pos.liquidity}; "
                f"receipts (raw minima): {inflows}; the position NFT stays\n" + note)


async def perform_permit2_approve(tool, params, execution_context=None):
    """``approve_token(via='permit2')``: one exact, expiring Permit2 grant to
    the pinned v4 PositionManager. The guard recognises this grant and no
    other (``liquidity_guard.permit2_grant_declared``)."""
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet.tokens import get_token_identity, normalize_address
    from tools.defi.trade_tool import _intent_idem, _unsupported_chain
    err = _unsupported_chain(params.chain)
    if err:
        return tool._ar(error=err)
    row = dex_registry.row_for(params.chain, "v4")
    if row is None or not row.permit2 or not row.position_manager:
        return tool._ar(error=f"no pinned Uniswap v4 deployment on {params.chain}")
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    try:
        token = normalize_address(params.token)
        spender = normalize_address(params.spender)
        npm = dex_registry.resolve_position_manager(params.chain, "v4")
    except ValueError as exc:
        return tool._ar(error=str(exc))
    if spender.lower() != npm.lower():
        return tool._ar(error=(f"refused: a Permit2 grant may name only the pinned v4 "
                               f"PositionManager {npm}; {spender} is not it"))
    ident = get_token_identity(params.chain, token)
    if ident.decimals is None:
        return tool._ar(error=f"{token} does not report decimals — refusing")
    amount_raw = int(Decimal(str(params.amount)) * 10 ** ident.decimals)
    if not 0 < amount_raw < V.MAX_UINT160:
        return tool._ar(error="refused: the Permit2 amount must be exact and below uint160 max")
    signer, gate = wallet.operational_signer(), wallet.policy
    rail = (tool._rail_factory or EvmRail)(chain=params.chain, signer=signer)
    try:
        rpc = rail._rpc
        dex_registry.verify_pins(rpc, params.chain, "v4")
    except Exception as exc:
        return tool._ar(error=f"refused: {exc}. RESULT: NOT SENT.")
    expiration = int(time.time()) + PERMIT2_GRANT_TTL_S
    data = V.encode_permit2_approve(token, npm, amount_raw, expiration)
    try:
        tx = rail.build_call(to=row.permit2, data=data, value=0)
    except Exception as exc:
        return tool._ar(error=f"could not build the transaction: {exc}")
    idem = _intent_idem("approve_permit2", params.chain, tx, execution_context)
    intent = tx_guard.TxIntent(
        chain=params.chain, token=token, to=npm, amount_raw=0,
        max_spend_usd=params.max_spend_usd,
        expected_allowance_grants=((token, npm, amount_raw),),
        is_allowance_op=True, idempotency_key=idem)
    header = (f"Permit2 grant {params.amount} of {token} to the v4 PositionManager {npm}, "
              f"expires in {PERMIT2_GRANT_TTL_S // 60} min\n")
    async with gate.reserve():
        return await tool._run_guarded(
            intent=intent, tx=tx, rail=rail, gate=gate, signer=signer,
            execution_context=execution_context, header=header,
            dry_run=params.dry_run, venue_action="approve", idem=idem,
            counterparty=npm)
