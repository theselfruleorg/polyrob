"""v4 liquidity on a Pons PoolKey: the lp_add plan, the LP program caps, and the
one Permit2 grant (048 phase 3; core handoff W6; 090 R2/R4).

How the ERC-20 leg moves (v4 PositionManager settles ERC-20 only through
Permit2; a signature permit is refused in this tree, so the grant is a
transaction):

1. ``approve_token(token, spender=Permit2, amount)`` — a standing ERC-20
   allowance to Permit2. Permit2 is not a route spender, so only a genuine
   owner turn may create it (CR-H02). The owner sizes it for the program.
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
from decimal import Decimal
from typing import Optional

from core.security.refusal_taint import PreconditionUnmet
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


def caps_refusal(gate, native_wei: int, balance_wei: int, now: Optional[float] = None) -> Optional[str]:
    """Why a v4 add of ``native_wei`` would break an LP program cap, or None.
    Sums ``native_raw`` of every booked ``lp_add`` in the durable ledger; an
    unreadable ledger refuses (unknown is not zero)."""
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
    rows = [e for e in gate.audit_log if e.get("action") == "lp_add" and "native_raw" in e]
    total = sum(int(e["native_raw"]) for e in rows)
    day = sum(int(e["native_raw"]) for e in rows if float(e.get("ts") or 0) >= now - 86400)
    eth = lambda w: f"{w / 1e18:.6g} ETH"
    if total + native_wei > program:
        return f"LP_ETH_CAP {eth(program)} would be exceeded ({eth(total)} deposited + {eth(native_wei)})"
    if daily > 0 and day + native_wei > daily:
        return f"LP_ETH_DAILY_CAP {eth(daily)} would be exceeded (24h {eth(day)} + {eth(native_wei)})"
    if balance_wei - native_wei < floor:
        return (f"LP_ETH_FLOOR: the wallet would keep {eth(balance_wei - native_wei)}, below "
                f"the {eth(floor)} floor")
    return None


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
    maxima = [min(d, u * 10000 // keep + 1) for d, u in zip(desired, (used0, used1))]
    price_token0 = chains.get(p.chain).wrapped_native if native0 else c0
    note = pool_price_check(p, price_token0, c1, dec0, dec1, st.sqrt_price_x96, price_fn)
    row = dex_registry.row_for(p.chain, "v4")
    out, held = [], []
    now = int(time.time())
    for cur, n, dec in ((c0, maxima[0], dec0), (c1, maxima[1], dec1)):
        if cur == V.ZERO:
            balance = int(rpc("eth_getBalance", [holder, "latest"]), 16)
        else:
            to_permit2 = R.allowance(rpc, p.chain, cur, holder, row.permit2)
            if to_permit2 < n:
                raise PreconditionUnmet(
                    f"Permit2 holds an ERC-20 allowance of {to_permit2} on {cur}, need {n}. The "
                    f"owner runs defi_trade.approve_token(chain={p.chain}, token={cur}, "
                    f"spender={row.permit2}, amount=<program size>) once (owner turn only).")
            granted, expiration = V.permit2_allowance(rpc, p.chain, holder, cur, npm)
            if granted < n or expiration <= now + DEADLINE_S:
                raise PreconditionUnmet(
                    f"Permit2 grant to the v4 PositionManager is {granted} (expires {expiration}), "
                    f"need {n}. Run defi_trade.approve_token(chain={p.chain}, token={cur}, "
                    f"spender={npm}, amount={Decimal(n) / 10 ** dec}, via='permit2') first.")
            balance = int(R.view(rpc, cur, A.NPM_BALANCE_OF, [holder]))
        if balance < n:
            raise PreconditionUnmet(f"insufficient balance of {cur}: {balance} < {n}")
        out.append((None if cur == V.ZERO else cur, n))
        held.append((None if cur == V.ZERO else cur, balance))
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
        return tool._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
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
