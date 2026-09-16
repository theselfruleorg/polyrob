"""Guarded Uniswap v3 liquidity writes. One transaction; approvals stay separate."""
from dataclasses import dataclass
from decimal import Decimal
import time
import uuid

from core.wallet import abi, chains, dex_registry, simulation, tx_guard, univ3_math as M
from tools.defi import lp_abi as A, lp_reads as R

FLAG = 'DEFI_LIQUIDITY_ENABLED'


def liquidity_enabled():
    from core.env import bool_env
    return bool_env(FLAG, False)


def encode(spec, values=()):
    return abi.encode_call(spec['name'], spec['inputs'], list(values))


def raw_amount(value, decimals):
    n = Decimal(str(value)) * 10 ** decimals
    if not n.is_finite() or n < 0 or n != int(n):
        raise ValueError('amount must be finite, nonnegative and fit the token decimals')
    return int(n)


def ticks(range_text, spacing, dec0, dec1, flipped=False):
    if range_text == 'full':
        return M.full_range_ticks(spacing)
    if range_text.startswith('ticks:'):
        lo, hi = map(int, range_text[6:].split(','))
        if lo % spacing or hi % spacing:
            raise ValueError('tick range must align to pool tick spacing')
    else:
        low, high = map(Decimal, range_text.split(','))
        if not low.is_finite() or not high.is_finite() or not 0 < low < high:
            raise ValueError('price range must satisfy 0 < low < high')
        if flipped:
            low, high = 1 / high, 1 / low
        lo = M.align_tick(M.price_to_tick(float(low), dec0, dec1), spacing)
        hi = M.align_tick(M.price_to_tick(float(high), dec0, dec1), spacing)
    if not M.MIN_TICK <= lo < hi <= M.MAX_TICK:
        raise ValueError('empty or out-of-bounds tick range')
    return lo, hi


@dataclass
class Plan:
    intent: tx_guard.TxIntent
    data: str
    value: int
    pool: str
    tokens: tuple
    decimals: tuple
    description: str


def _pons_warning(rpc, chain, tokens, holder, fragment):
    if chain != 'robinhood':
        return ''
    from tools.launchpad import pons
    warning = ''
    for token in tokens:
        record = pons.launched_token(rpc, token)
        if record:
            state = pons.curve_state(rpc, record['curve'], recipient=holder)
            if not state.graduated and not fragment:
                raise ValueError(f'{token} is still on its bonding curve; a separate pool fragments its market. Set fragment=true only if intended.')
            warning = ('Pons graduated pools pay LP fee 0; their hook pays creator escrow. '
                       'This v3 pool is a separate market with its own fee tier.\n')
    return warning


def prepare_add(p, rpc, holder, npm):
    chain_row = chains.get(p.chain)
    from core.wallet.tokens import normalize_address
    native_a, native_b = p.token_a.lower() == 'native', p.token_b.lower() == 'native'
    ta = chain_row.wrapped_native if native_a else normalize_address(p.token_a)
    tb = chain_row.wrapped_native if native_b else normalize_address(p.token_b)
    t0, t1, flipped = M.sort_tokens(ta, tb)
    ds = tuple(int(R.view(rpc, t, A.ERC20_DECIMALS)) for t in (t0, t1))
    amounts = (p.amount_b, p.amount_a) if flipped else (p.amount_a, p.amount_b)
    native = (native_b, native_a) if flipped else (native_a, native_b)
    desired = [raw_amount(x, d) for x, d in zip(amounts, ds)]
    if p.fee not in A.FEE_TIERS:
        raise ValueError('fee must be 100, 500, 3000 or 10000 (millionths, not bps)')
    warning = _pons_warning(rpc, p.chain, (t0, t1), holder, p.fragment)
    pool = R.pool_address(rpc, p.chain, t0, t1, p.fee)
    inner, events = [], [(npm, A.TOPIC_INCREASE_LIQUIDITY)]
    ps = R.pool_state(rpc, p.chain, pool) if pool else None
    if ps is None or ps.sqrt_price_x96 == 0:
        if p.initial_price is None:
            raise ValueError('pool does not exist or is uninitialized; pass initial_price (token_b per token_a) to create it. Read Liquidity in treasury-trading first.')
        price = Decimal(str(p.initial_price))
        if not price.is_finite() or price <= 0:
            raise ValueError('initial_price must be finite and positive')
        sqrt = M.sqrt_price_from_price(float(1 / price if flipped else price), *ds)
        inner.append(encode(A.NPM_CREATE_AND_INIT, (t0, t1, p.fee, sqrt)))
        if pool is None:
            events.append((dex_registry.row_for(p.chain, 'v3').factory, A.TOPIC_POOL_CREATED))
    else:
        sqrt = ps.sqrt_price_x96
    lo, hi = ticks(p.range, A.FEE_TIERS[p.fee], *ds, flipped)
    if p.token_id is not None:
        if str(R.view(rpc, npm, A.NPM_OWNER_OF, [p.token_id])).lower() != holder.lower():
            raise ValueError('wallet does not own this position')
        pos = R.position(rpc, p.chain, p.token_id)
        if (pos.token0.lower(), pos.token1.lower(), pos.fee) != (t0.lower(), t1.lower(), p.fee):
            raise ValueError('token pair/fee does not match the existing position')
        lo, hi = pos.tick_lower, pos.tick_upper
    liquidity = M.liquidity_for_amounts(sqrt, M.sqrt_price_at_tick(lo), M.sqrt_price_at_tick(hi), *desired)
    if not 0 < liquidity <= A.MAX_UINT128:
        raise ValueError('deposit produces zero or overflowing liquidity')
    expected = M.amounts_for_liquidity(sqrt, M.sqrt_price_at_tick(lo), M.sqrt_price_at_tick(hi), liquidity)
    if not any(expected):
        raise ValueError('deposit is too small to measure')
    # Force an unused leg to zero in both calldata and intent. A price move that
    # would require it then reverts instead of silently spending an undeclared leg.
    desired = [n if e else 0 for n, e in zip(desired, expected)]
    minimums = [max(1, n * (10000 - p.slippage_bps) // 10000) if n else 0 for n in expected]
    out, held = [], []
    for t, n, nat, dec in zip((t0, t1), desired, native, ds):
        if not n:
            continue
        if not nat:
            available = R.allowance(rpc, p.chain, t, holder, npm)
            if available < n:
                raise ValueError(f'allowance short: {available}, need {n}. Run defi_trade.approve_token(chain={p.chain}, token={t}, spender={npm}, amount={Decimal(n) / 10**dec}) first; one exact approval per transaction.')
            balance = int(R.view(rpc, t, A.NPM_BALANCE_OF, [holder]))
        else:
            balance = int(rpc('eth_getBalance', [holder, 'latest']), 16)
        if balance < n:
            raise ValueError(f'insufficient balance of {t}')
        out.append((None if nat else t, n))
        held.append((None if nat else t, balance))
    deadline = int(time.time()) + 600
    if p.token_id is None:
        inner.append(encode(A.NPM_MINT, [(t0, t1, p.fee, lo, hi, *desired, *minimums, holder, deadline)]))
    else:
        inner.append(encode(A.NPM_INCREASE, [(p.token_id, *desired, *minimums, deadline)]))
    value = sum(n for t, n in out if t is None)
    if value:
        inner.append(encode(A.NPM_REFUND_ETH))
    intent = tx_guard.TxIntent(chain=p.chain, token=None, to=npm, amount_raw=0,
        max_spend_usd=p.max_spend_usd, is_liquidity_op=True,
        lp_outflows=tuple(out), lp_held_balances=tuple(held), watch_spenders=(npm,),
        lp_position=(npm, p.token_id), lp_position_effect='mint' if p.token_id is None else 'hold',
        expected_events=tuple(events), idempotency_key='lp_add:' + uuid.uuid4().hex)
    return Plan(intent, _multicall(inner), value, pool or 'new pool', (t0, t1), ds,
        f'pool: {pool or "WILL BE CREATED"}; ticks [{lo}, {hi}]; fee {p.fee / 10000:g}%\n'
        f'outflows (raw maxima): {out}\n' + warning)


def _multicall(inner):
    return inner[0] if len(inner) == 1 else encode(A.NPM_MULTICALL, [[bytes.fromhex(c[2:]) for c in inner]])


def prepare_exit(p, rpc, holder, npm, verb):
    if str(R.view(rpc, npm, A.NPM_OWNER_OF, [p.token_id])).lower() != holder.lower():
        raise ValueError('wallet does not own this position')
    pv = R.position(rpc, p.chain, p.token_id)
    inner, events = [], [(npm, A.TOPIC_COLLECT)]
    # Read the actual collect return, not the nominal fee-growth estimate:
    # core rounding can leave that estimate several raw units too high.
    collectible = R.collectible_fees(rpc, p.chain, p.token_id, holder)
    mins = [max(1, n * 98 // 100) if n else 0 for n in collectible]
    burn = verb == 'lp_remove' and p.burn
    if verb == 'lp_remove':
        if burn and p.liquidity_pct != 100:
            raise ValueError('burn requires liquidity_pct=100')
        liq = pv.liquidity * p.liquidity_pct // 100
        if not liq:
            raise ValueError('position has no removable liquidity; use lp_collect for tokens owed')
        ps = R.pool_state(rpc, p.chain, pv.pool)
        amounts = M.amounts_for_liquidity(ps.sqrt_price_x96,
            M.sqrt_price_at_tick(pv.tick_lower), M.sqrt_price_at_tick(pv.tick_upper), liq)
        minimums = [max(1, n * (10000 - p.slippage_bps) // 10000) if n else 0 for n in amounts]
        inner.append(encode(A.NPM_DECREASE, [(p.token_id, liq, *minimums, int(time.time()) + 600)]))
        mins = [m + fee for m, fee in zip(minimums, mins)]
        events.append((npm, A.TOPIC_DECREASE_LIQUIDITY))
    if not any(mins):
        raise ValueError('nothing to collect: no fees or withdrawable amounts')
    # Collect both tokens, including new fees; declared minima bound the receipt.
    inner.append(encode(A.NPM_COLLECT, [(p.token_id, holder, A.MAX_UINT128, A.MAX_UINT128)]))
    if burn:
        inner.append(encode(A.NPM_BURN, [p.token_id]))
    intent = tx_guard.TxIntent(chain=p.chain, token=None, to=npm, amount_raw=0,
        max_spend_usd=p.max_spend_usd, is_liquidity_op=True,
        lp_inflows=((pv.token0, mins[0]), (pv.token1, mins[1])),
        lp_position=(npm, p.token_id), lp_position_effect='burn' if burn else 'hold',
        expected_events=tuple(events), idempotency_key=verb + ':' + uuid.uuid4().hex)
    return Plan(intent, _multicall(inner), 0, pv.pool, (pv.token0, pv.token1),
        (pv.dec0, pv.dec1), f'pool: {pv.pool}; position: {p.token_id}\nreceipts (raw minima): {intent.lp_inflows}\n')


def receipt_position(plan, raw_receipt, holder):
    """Use the LANDED mint id; another mint can race our simulation."""
    if not isinstance(raw_receipt, dict) or not isinstance(raw_receipt.get('logs'), list):
        raise ValueError('receipt logs unavailable; position accounting unverified')
    logs = raw_receipt['logs']
    events = simulation._holder_events(logs, holder)
    observed = {(str(l.get('address', '')).lower(), str(l['topics'][0]).lower()) for l in logs if l.get('topics')}
    for emitter, topic in plan.intent.expected_events:
        if (emitter.lower(), topic.lower()) not in observed:
            raise ValueError('receipt missing required liquidity event')
    npm, token_id = plan.intent.lp_position
    if plan.intent.lp_position_effect == 'mint':
        if (len(events.nft_in) != 1 or events.nft_out or events.nft_in[0][:3] !=
                (npm.lower(), 'erc721', '0x' + '0' * 40)):
            raise ValueError('receipt did not mint exactly one pinned position')
        token_id = events.nft_in[0][3]
    elif plan.intent.lp_position_effect == 'burn':
        if events.nft_in or events.nft_out != ((npm.lower(), 'erc721', '0x' + '0' * 40, token_id, 1),):
            raise ValueError('receipt did not burn the declared position')
    elif events.nft_in or events.nft_out:
        raise ValueError('receipt unexpectedly transferred an NFT')
    from core.wallet.liquidity_guard import assert_position_events
    assert_position_events(plan.intent, logs, token_id)
    from tools.defi.lp_receipts import assert_fungible_receipt
    assert_fungible_receipt(plan.intent, logs, holder)
    return token_id



async def _perform(tool, p, ctx, verb):
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet import tx_notify
    from tools.defi.deploy_verb import _refuse_non_owner_turn, _refuse_paused
    from tools.defi.trade_tool import _unsupported_chain
    if not liquidity_enabled():
        return tool._ar(error=f'liquidity writes are off; set {FLAG}=true. Nothing was broadcast.')
    err = _refuse_non_owner_turn(ctx, verb) or _unsupported_chain(p.chain)
    if err:
        return tool._ar(error=err)
    if p.protocol != 'v3':
        return tool._ar(error='Uniswap v4 requires the phase-3 Permit2 rail; v3 only today. Nothing was broadcast.')
    paused = _refuse_paused()
    if paused:
        return tool._ar(error=paused)
    wallet = tool._get_wallet()
    if wallet is None:
        return tool._ar(error='agent wallet not enabled (AGENT_WALLET_ENABLED)')
    signer, gate = wallet.operational_signer(), wallet.policy
    try:
        rail = (tool._rail_factory or EvmRail)(chain=p.chain, signer=signer)
        rpc = getattr(tool, '_lp_rpc', None) or rail._rpc
        npm = dex_registry.resolve_position_manager(p.chain, 'v3')
        dex_registry.verify_pins(rpc, p.chain, 'v3')
        plan = prepare_add(p, rpc, signer.address, npm) if verb == 'lp_add' else prepare_exit(p, rpc, signer.address, npm, verb)
    except Exception as exc:
        return tool._ar(error=f'refused: {exc}. RESULT: NOT SENT.')
    async with gate.reserve():
        from tools.controller.action_registration import _is_forged_or_autonomous_turn
        try:
            tx = rail.build_call(to=npm, data=plan.data, value=plan.value)
            decision = (tool._guard_fn or tx_guard.authorize)(plan.intent, tx,
                holder=signer.address, gate=gate, execution_context=ctx, tool_self=tool,
                price_fn=tool._price, fallback_price_fn=tool._fallback_price,
                forged_fn=_is_forged_or_autonomous_turn, liquidity_rpc=rpc)
            header = f'{verb} on {p.chain} (Uniswap v3)\n{plan.description}guard: {decision.reason}; value: {decision.amount_usd}; lane: {decision.lane}\n'
            if not decision.allowed:
                return tool._ar(content=header + 'RESULT: NOT SENT.')
            if not decision.sim_gas_used:
                raise ValueError('simulation did not measure gas; refusing to broadcast unsized')
            tx = rail.size_gas(tx, decision.sim_gas_used)
            if p.dry_run:
                note = f'would mint position #{decision.position_token_id} (prediction only)\n' if plan.intent.lp_position_effect == 'mint' else ''
                return tool._ar(content=header + note + 'RESULT: DRY RUN. Re-run with dry_run=false to send.')
            # Re-check the owner's pause immediately before signing.
            if _refuse_paused():
                raise ValueError('owner paused spending before broadcast')
            tx_hash = rail.sign_and_send(tx)
        except Exception as exc:
            return tool._ar(error=f'refused before broadcast: {exc}')
        tool._notify_tx(ctx, tx_notify.TxNotice(verb=verb, route=p.chain + ':v3',
            chain=p.chain, amount_in=plan.description, usd=decision.amount_usd,
            tx_ref=tx_hash, lane=decision.lane), settled=False)
        # Once submitted, ALWAYS book the cap, even if polling or receipt parsing
        # fails. Never describe a receipt failure as "nothing sent".
        asset, positions, detail, state = None, [], '', tx_notify.STATE_IN_FLIGHT
        try:
            receipt = rail.await_receipt(tx_hash)
            detail = receipt.status
            if receipt.status == 'success':
                state = tx_notify.STATE_CONFIRMED
                raw = rpc('eth_getTransactionReceipt', [tx_hash])
                token_id = receipt_position(plan, raw, signer.address)
                # The token-keyed book cannot represent an LP claim separately.
                # Adding the legs would double-count previously acquired tokens;
                # removing them on withdrawal would erase unrelated holdings.
                # Keep NFT/transaction telemetry until an LP-specific basis exists.
                asset = f'erc721:{npm.lower()}:{token_id}'
                detail += f'; position #{token_id}'
            elif receipt.status == 'failed':
                state = tx_notify.STATE_REVERTED
        except Exception as exc:
            detail = f'SUBMITTED; receipt/accounting unverified ({exc}). Do not retry blindly.'
        gate.record(venue='defi', action=verb, amount_usd=decision.amount_usd,
            counterparty=plan.pool, idempotency_key=plan.intent.idempotency_key,
            result_ref=tx_hash, chain=p.chain, asset=asset, positions=positions)
        tool._notify_tx(ctx, tx_notify.TxNotice(verb=verb, route=p.chain + ':v3',
            chain=p.chain, amount_in=plan.description, usd=decision.amount_usd,
            tx_ref=tx_hash, state=state, detail=detail, ledger_recorded=True), settled=True)
        return tool._ar(content=header + f'RESULT: {detail}\ntx: {tx_hash}')


async def perform_lp_add(tool, params, execution_context=None):
    return await _perform(tool, params, execution_context, 'lp_add')


async def perform_lp_remove(tool, params, execution_context=None):
    return await _perform(tool, params, execution_context, 'lp_remove')


async def perform_lp_collect(tool, params, execution_context=None):
    return await _perform(tool, params, execution_context, 'lp_collect')
