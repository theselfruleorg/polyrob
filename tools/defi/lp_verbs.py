"""Guarded Uniswap liquidity writes. One transaction; approvals stay separate.

v3: add/remove/collect. v4 (048 phase 3): lp_add of a new full-range position
on a Pons PoolKey, (115 W4) lp_collect of a position's fees — from the
wallet, or with ``account=``/``nft=`` from an NFT's account that owns the
position — and (117 W7) lp_remove from the wallet, which keeps the position
NFT; see ``tools/defi/lp_v4_verbs.py``."""
import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
import time
import json
import math
import re
from typing import Optional

from core.wallet import abi, chains, dex_registry, simulation, tx_guard, univ3_math as M
from tools.defi import lp_abi as A, lp_reads as R

FLAG = 'DEFI_LIQUIDITY_ENABLED'


def liquidity_enabled():
    from core.env import bool_env
    return bool_env(FLAG, False)


def encode(spec, values=()):
    return abi.encode_call(spec['name'], spec['inputs'], list(values))


def raw_amount(value, decimals):
    from core.wallet.tokens import raw_amount as to_raw
    return to_raw(value, decimals)


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
    #: 117: the owner's declaration a v4 seed plan was built from (lp_v4_seed).
    seed: Optional[dict] = None


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


def pool_price_check(p, t0, t1, dec0, dec1, sqrt_price_x96, price_fn):
    """CR-M12: the pool's spot price against an INDEPENDENT one, or refuse.

    LP minimums are derived from the pool's own spot price, which anyone with
    capital can move for the length of our transaction. When both legs have an
    independent price and the pool drifts past the swap rail's tolerance, the
    deposit/withdrawal is refused exactly like a disagreeing swap route. No
    independent price for a leg: a dry run names the missing check; a live
    operation refuses rather than trusting minimums derived from that pool.
    """
    from tools.defi.trade_tool import _route_drift_max_pct
    try:
        raw0, raw1 = price_fn(p.chain, t0), price_fn(p.chain, t1)
        if isinstance(raw0, bool) or isinstance(raw1, bool):
            raise ValueError('a boolean is not a price')
        p0, p1 = float(raw0), float(raw1)
        valid = all(math.isfinite(v) and v > 0 for v in (p0, p1, sqrt_price_x96))
    except Exception:
        valid = False
    if not valid:
        message = 'pool price check: UNAVAILABLE (a finite independent price is required for both legs)'
        if not p.dry_run:
            raise ValueError(message + '; live liquidity operation refused')
        return message + '\n'
    s = sqrt_price_x96 / M.Q96
    pool = (s * s) * (10 ** dec0) / (10 ** dec1)          # token1 per token0
    independent = p0 / p1
    drift = abs(pool - independent) / independent * 100.0
    limit = _route_drift_max_pct()
    if not math.isfinite(drift) or drift > limit:
        raise ValueError(
            f'pool price {pool:.8g} disagrees with the independent price '
            f'{independent:.8g} by {drift:.2f}% (limit {limit:.0f}%); a thin or '
            f'manipulated pool extracts value through LP minimums. Nothing was built')
    return f'pool price check: AGREES ({drift:.2f}% drift)\n'


def exit_price_fn(tool):
    """The pool check's price for an EXIT (lp_remove): spend-grade, else the
    exit-grade price the swap rail uses to keep a held position closable.

    An exit withdraws the wallet's own liquidity. Requiring a spend-grade price
    for both legs locked the agent's principal in any pool whose token has only
    an exit-grade (low-confidence, measured-depth) price, e.g. its own launched
    token. With neither price the live exit still refuses."""
    def _fn(chain, addr):
        try:
            value = tool._price(chain, addr)
        except Exception:
            value = None
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value) and value > 0:
            return value
        fallback = getattr(tool, '_fallback_price', None)
        return fallback(chain, addr) if callable(fallback) else value
    return _fn


def prepare_add(p, rpc, holder, npm, price_fn=None, grants=None):
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
            raise ValueError('pool does not exist or is uninitialized; pass initial_price (token_b per token_a) to create it. Read the defi-liquidity skill first.')
        price = Decimal(str(p.initial_price))
        if not price.is_finite() or price <= 0:
            raise ValueError('initial_price must be finite and positive')
        sqrt = M.sqrt_price_from_price(float(1 / price if flipped else price), *ds)
        inner.append(encode(A.NPM_CREATE_AND_INIT, (t0, t1, p.fee, sqrt)))
        if pool is None:
            events.append((dex_registry.row_for(p.chain, 'v3').factory, A.TOPIC_POOL_CREATED))
    else:
        sqrt = ps.sqrt_price_x96
        warning += pool_price_check(p, t0, t1, *ds, sqrt, price_fn)
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
            if available < n and grants is not None:
                grants.append((t, n, dec))   # 126 wave 4: the live add makes it first
            elif available < n:
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
        expected_events=tuple(events), idempotency_key=None)
    return Plan(intent, _multicall(inner), value, pool or 'new pool', (t0, t1), ds,
        f'pool: {pool or "WILL BE CREATED"}; ticks [{lo}, {hi}]; fee {p.fee / 10000:g}%\n'
        f'outflows (raw maxima): {out}\n' + warning)


def _multicall(inner):
    return inner[0] if len(inner) == 1 else encode(A.NPM_MULTICALL, [[bytes.fromhex(c[2:]) for c in inner]])


def prepare_exit(p, rpc, holder, npm, verb, price_fn=None):
    if str(R.view(rpc, npm, A.NPM_OWNER_OF, [p.token_id])).lower() != holder.lower():
        raise ValueError('wallet does not own this position')
    pv = R.position(rpc, p.chain, p.token_id)
    inner, events = [], [(npm, A.TOPIC_COLLECT)]
    note = ''
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
        note = pool_price_check(p, pv.token0, pv.token1, pv.dec0, pv.dec1,
                                ps.sqrt_price_x96, price_fn)
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
        expected_events=tuple(events), idempotency_key=None)
    return Plan(intent, _multicall(inner), 0, pv.pool, (pv.token0, pv.token1),
        (pv.dec0, pv.dec1), f'pool: {pv.pool}; position: {p.token_id}\nreceipts (raw minima): {intent.lp_inflows}\n' + note)


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
    from core.wallet.liquidity_guard import assert_position_events, protocol_of
    # v4: the landed events must equal the SIGNED bytes (117), as in the guard.
    signed = {'data': plan.data} if protocol_of(plan.intent) == 'v4' else None
    assert_position_events(plan.intent, logs, token_id, signed)
    from tools.defi.lp_receipts import assert_fungible_receipt
    assert_fungible_receipt(plan.intent, logs, holder)
    return token_id



def _pause_refusal(ctx, *, entry: bool):
    """Why a pause refuses this liquidity write, or None (the one authorizer)."""
    from core.money.authorize import SpendIntent, authorize_spend
    from tools.controller.turn_origin import (
        _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
    verdict = authorize_spend(SpendIntent.pause_recheck(entry=entry), ctx,
                              forged_fn=_is_forged_or_autonomous_turn,
                              autonomous_ok_fn=_is_autonomous_goal_turn)
    return verdict.reason if verdict.refused else None


async def _perform(tool, p, ctx, verb):
    from core.wallet.broadcast.evm import EvmRail
    from core.wallet import tx_notify
    from core.money.authorize import SpendIntent, authorize_spend
    from tools.defi.trade_tool import _unsupported_chain
    if not liquidity_enabled():
        return tool._ar(error=f'liquidity writes are off; set {FLAG}=true. Nothing was broadcast.')
    # 067 P1b: the kernel's leaf + principal steps first, as before.
    verdict = authorize_spend(SpendIntent.verb_gate(verb), ctx)
    err = (verdict.reason if verdict.refused else None) or _unsupported_chain(p.chain)
    if err:
        return tool._ar(error=err)
    if p.protocol not in ('v3', 'v4'):
        return tool._ar(error=f'unknown protocol {p.protocol!r}; v3 or v4. Nothing was broadcast.')
    from tools.defi import account_mode
    v4_collect = p.protocol == 'v4' and verb == 'lp_collect'
    if account_mode.requested(p) and not v4_collect:
        return tool._ar(error=("account=/nft= works for lp_collect with protocol='v4' only; "
                               "every other liquidity verb acts from the wallet. Nothing was "
                               "broadcast."))
    # CR-L21: a deposit OPENS a position, so the scoped trade_entry pause binds
    # it; a removal or a fee collection is an exit and stays available.
    why = _pause_refusal(ctx, entry=verb == 'lp_add')
    if why:
        return tool._ar(error=why)
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    signer, gate = wallet.operational_signer(), wallet.policy
    # 115 W4: `account=` / `nft=` — the NFT's account owns the position and receives
    # the fees; the wallet signs account.execute(PositionManager, …) as the NFT's owner.
    held = None
    if account_mode.requested(p):
        held, why = account_mode.resolve(p, p.chain, signer.address, rpc=tool._account_rpc)
        if why:
            return tool._ar(error=why)
    holder = held.account if held is not None else signer.address
    try:
        rail = (tool._rail_factory or EvmRail)(chain=p.chain, signer=signer)
        rpc = getattr(tool, '_lp_rpc', None) or rail._rpc
        npm = dex_registry.resolve_position_manager(p.chain, p.protocol)
        dex_registry.verify_pins(rpc, p.chain, p.protocol)
        if v4_collect:
            from tools.defi.lp_v4_verbs import prepare_collect_v4
            plan = prepare_collect_v4(p, rpc, holder, npm)
        elif p.protocol == 'v4' and verb == 'lp_remove':
            from tools.defi.lp_v4_verbs import prepare_remove_v4
            plan = prepare_remove_v4(p, rpc, signer.address, npm, price_fn=exit_price_fn(tool))
        elif p.protocol == 'v4':
            from tools.defi import lp_v4_seed
            from tools.defi.lp_v4_verbs import prepare_add_v4, prepare_increase_v4
            if lp_v4_seed.proposes(p):
                # 117: a PoolKey named in full is a PROPOSAL to the owner, never a send.
                return await lp_v4_seed.raise_card(tool, p, ctx, rpc, holder=signer.address)
            if p.token_id is not None and p.seed is None:
                # 118 W6: more liquidity into a held position, in its own range.
                plan = prepare_increase_v4(p, rpc, signer.address, npm, price_fn=tool._price)
            else:
                plan = (lp_v4_seed.prepare_seed_v4(p, rpc, signer.address, npm,
                                                   lp_v4_seed.load(p, ctx))
                        if p.seed is not None else
                        prepare_add_v4(p, rpc, signer.address, npm, price_fn=tool._price))
        else:
            plan = (prepare_add(p, rpc, signer.address, npm, price_fn=tool._price)
                    if verb == 'lp_add' else
                    prepare_exit(p, rpc, signer.address, npm, verb,
                                 price_fn=exit_price_fn(tool)))
    except Exception as exc:
        return tool._ar(error=f'refused: {exc}. RESULT: NOT SENT.')
    async with gate.reserve():
        from tools.controller.action_registration import (
            _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
        cap_note = ''
        if p.protocol == 'v4' and verb == 'lp_add':
            # 090 R4 caps: a refusal before a broadcast; a note on a dry run, so
            # the guard's own judgement stays visible while the cap is unset.
            from tools.defi.lp_v4_verbs import caps_refusal
            native_held = dict(plan.intent.lp_held_balances).get(None, 0)
            why = caps_refusal(gate, plan.value, native_held)
            if why and not p.dry_run:
                return tool._ar(error=f'refused: {why}. RESULT: NOT SENT.')
            cap_note = f'LP caps: {why or "within LP_ETH_CAP / LP_ETH_DAILY_CAP / LP_ETH_FLOOR"}\n'
        journal_entry, journal_skipped, guard_kw = None, '', {}
        claimed = False
        try:
            tx = rail.build_call(to=npm, data=plan.data, value=plan.value)
            if held is not None:
                # J1: the journal entry rides the same account batch (a pinned JournalLog).
                journal_entry, journal_skipped = account_mode.prepare_journal(
                    held, signer, kind='tend', text=f'{verb} v4 position #{p.token_id} on {p.chain}',
                    rpc=tool._account_rpc)
                tx, acct_state = account_mode.wrap(rail, tx, held, tool._account_rpc,
                                                   journal=journal_entry)
                plan.intent = account_mode.intent_for(plan.intent, held, acct_state,
                                                      journal=journal_entry is not None)
                if tool._account_rpc is not None:
                    guard_kw['account_rpc'] = tool._account_rpc
            from tools.defi.call_verb import intent_idempotency_key
            # The builder refreshes deadlines on retry. Key the requested
            # operation, not that clock-derived calldata, at this nonce.
            request_data = json.dumps(p.model_dump(exclude={"dry_run", "max_spend_usd"}),
                                      sort_keys=True, separators=(",", ":"))
            plan.intent = replace(plan.intent, idempotency_key=intent_idempotency_key(
                verb, chain=p.chain, to=npm, data=request_data, value_wei=plan.value,
                tx=tx, execution_context=ctx))
            # CR-M10: simulation, signing RPC and receipt polling run off the
            # event loop so the held reservation never freezes other sessions.
            decision = await asyncio.to_thread(
                tool._guard_fn or tx_guard.authorize, plan.intent, tx,
                holder=signer.address, gate=gate, execution_context=ctx, tool_self=tool,
                price_fn=tool._price, fallback_price_fn=tool._fallback_price,
                forged_fn=_is_forged_or_autonomous_turn,
                autonomous_ok_fn=_is_autonomous_goal_turn, dry_run=bool(p.dry_run), liquidity_rpc=rpc, **guard_kw)
            header = (f'{verb} on {p.chain} (Uniswap {p.protocol})\n'
                      + (account_mode.header_line(held) if held is not None else '')
                      + f'{plan.description}{cap_note}guard: {decision.reason}; value: {decision.amount_usd}; lane: {decision.lane}\n')
            if not decision.allowed:
                return tool._ar(content=header + 'RESULT: NOT SENT.')
            from core.wallet.broadcast.evm import is_account_call
            if not decision.sim_gas_used and not is_account_call(tx):
                raise ValueError('simulation did not measure gas; refusing to broadcast unsized')
            tx = rail.size_gas(tx, decision.sim_gas_used)
            if p.dry_run:
                note = f'would mint position #{decision.position_token_id} (prediction only)\n' if plan.intent.lp_position_effect == 'mint' else ''
                from tools.base_tool import dry_run_result
                return dry_run_result(tool, header + note + 'RESULT: DRY RUN. Re-run with dry_run=false to send.',
                                      decision.amount_usd)
            # Re-check the owner's pause immediately before signing.
            if _pause_refusal(ctx, entry=verb == 'lp_add'):
                raise ValueError('owner paused spending before broadcast')
            if plan.seed is not None:
                # 117: a declaration is ONE use — taken here, just before the signature.
                from tools.defi import lp_v4_seed
                lp_v4_seed.claim(plan.seed)
                claimed = True
            tx_hash = await asyncio.to_thread(rail.sign_and_send, tx)
        except Exception as exc:
            from core.wallet.broadcast.evm import broadcast_failure_text, outcome_unknown
            if claimed:
                from tools.defi import lp_v4_seed
                lp_v4_seed.unsent(plan.seed, unknown=outcome_unknown(exc), why=str(exc))
            if outcome_unknown(exc):
                return tool._ar(error=broadcast_failure_text(exc))
            return tool._ar(error=f'refused before broadcast: {exc}')
        tool._notify_tx(ctx, tx_notify.TxNotice(verb=verb, route=f'{p.chain}:{p.protocol}',
            chain=p.chain, amount_in=plan.description, usd=decision.amount_usd,
            tx_ref=tx_hash, lane=decision.lane), settled=False)
        # Once submitted, ALWAYS book the cap, even if polling or receipt parsing
        # fails. Never describe a receipt failure as "nothing sent".
        # v4: stamp the native sent (the declared maximum — the SWEEP refund is
        # not subtracted, so the LP caps over-count, never under-count).
        extra = {'native_raw': plan.value} if p.protocol == 'v4' else {}
        if p.protocol == 'v4' and verb == 'lp_remove':
            # 118 W1: the native this withdrawal returns to the LP program cap.
            extra['native_back_raw'] = dict(plan.intent.lp_inflows).get(None, 0)
        if held is not None:
            extra['account'] = held.account
        asset, positions, detail, state = None, [], '', tx_notify.STATE_IN_FLIGHT
        receipt, landed_id = None, None
        # CLI1: a cancel in the receipt wait or the position read-back records the
        # broadcast first (no asset — nothing is measured yet) — the ONE seam.
        from tools.defi.receipt_wait import record_on_interrupt
        with record_on_interrupt(
                tx_hash, gate=gate, tool=tool, execution_context=ctx,
                record_kw=dict(venue='defi', action=verb, amount_usd=decision.amount_usd,
                               counterparty=plan.pool,
                               idempotency_key=plan.intent.idempotency_key,
                               result_ref=tx_hash, chain=p.chain, asset=None,
                               positions=[], **extra),
                notice_kw=dict(verb=verb, route=f'{p.chain}:{p.protocol}', chain=p.chain,
                               amount_in=plan.description, usd=decision.amount_usd)):
            try:
                receipt = await asyncio.to_thread(rail.await_receipt, tx_hash)
                detail = receipt.status
                if receipt.status == 'success':
                    state = tx_notify.STATE_CONFIRMED
                    raw = await asyncio.to_thread(rpc, 'eth_getTransactionReceipt', [tx_hash])
                    token_id = receipt_position(plan, raw, holder)
                    # The token-keyed book cannot represent an LP claim separately.
                    # Adding the legs would double-count previously acquired tokens;
                    # removing them on withdrawal would erase unrelated holdings.
                    # Keep NFT/transaction telemetry until an LP-specific basis exists.
                    asset = f'erc721:{npm.lower()}:{token_id}'
                    detail += f'; position #{token_id}'
                    landed_id = token_id
                elif receipt.status == 'failed':
                    state = tx_notify.STATE_REVERTED
            except Exception as exc:
                detail = f'SUBMITTED; receipt/accounting unverified ({exc}). Do not retry blindly.'
        if plan.seed is not None:
            from tools.defi import lp_v4_seed
            detail += lp_v4_seed.settle(plan.seed, rpc, p.chain, tx_hash=tx_hash, holder=holder,
                                        token_id=landed_id,
                                        reverted=state == tx_notify.STATE_REVERTED)
        from tools.defi.receipt_wait import book_after_receipt
        book_after_receipt(gate, receipt, venue='defi', action=verb,
            amount_usd=decision.amount_usd, counterparty=plan.pool,
            idempotency_key=plan.intent.idempotency_key,
            result_ref=tx_hash, chain=p.chain, asset=asset, positions=positions, **extra)
        tool._notify_tx(ctx, tx_notify.TxNotice(verb=verb, route=f'{p.chain}:{p.protocol}',
            chain=p.chain, amount_in=plan.description, usd=decision.amount_usd,
            tx_ref=tx_hash, state=state, detail=detail, ledger_recorded=True), settled=True)
    journal = ''
    if held is not None:
        journal = account_mode.journal_line(
            held, signer, kind='tend',
            text=f'{verb} v4 position #{p.token_id} on {p.chain}: tx {tx_hash} ({detail})',
            refs=(tx_hash,), entry=journal_entry, landed=state != tx_notify.STATE_REVERTED,
            skipped=journal_skipped, rpc=tool._account_rpc)
    return tool._ar(content=header + f'RESULT: {detail}\ntx: {tx_hash}' + journal)


def _move_precheck(tool, p, row, rpc, holder, npm, gate, pos, facts=None):
    """Why the MINT of a move would not pass once the withdrawal has landed, or
    None. A dry run of the mint cannot answer this — the wallet does not hold the
    amounts yet — so the checks run here against what the withdrawal returns at
    least: the pool and the gap, the allowances, the LP caps, the USD limits.
    ``facts`` (a dict) gets ``mint_usd``, the mint's value at independent prices,
    when every leg has one."""
    from tools.defi import lp_v4 as V, lp_v4_seed
    from tools.defi.lp_v4_move import withdrawn_minimum
    from tools.defi.lp_v4_verbs import caps_refusal
    f = row["fields"]
    key = lp_v4_seed.S.key_of(f)
    c0, c1 = key["currency0"], key["currency1"]
    st = V.pool_state(rpc, p.chain, f["pool_id"])
    back = withdrawn_minimum(f, st.sqrt_price_x96, pos.tick_lower, pos.tick_upper)
    credit = {c0: back[0], c1: back[1]}
    try:
        plan = lp_v4_seed.prepare_seed_v4(p.model_copy(update={"dry_run": True}), rpc, holder,
                                          npm, row, credit=credit, grants_follow=p.dry_run)
    except Exception as exc:
        return str(exc)
    if facts is not None:
        facts["grant_notes"] = [ln for ln in plan.description.splitlines()
                                if "makes the Permit2 grant" in ln]
    native_back = back[0] if c0 == V.ZERO else 0
    balance = int(rpc("eth_getBalance", [holder, "latest"]), 16) + native_back
    why = caps_refusal(gate, plan.value, balance,
                       returning=(f"erc721:{npm.lower()}:{f['from_token_id']}", native_back))
    if why:
        return why
    usd = 0.0
    dec_of = {None if c0 == V.ZERO else c0: plan.decimals[0], c1: plan.decimals[1]}
    for token, raw in plan.intent.lp_outflows:
        dec = dec_of[token]
        if not raw:
            continue
        try:
            px = float(tool._price(p.chain, token or chains.get(p.chain).wrapped_native))
        except Exception:
            px = 0.0
        if not (math.isfinite(px) and px > 0):
            return None        # no independent price: the guard judges the mint itself
        usd += raw / 10 ** dec * px
    if facts is not None:
        facts["mint_usd"] = round(usd, 2)
    if usd > float(p.max_spend_usd):
        return (f"the mint moves about ${usd:,.2f} and max_spend_usd is ${p.max_spend_usd:,.2f}; "
                f"pass a max_spend_usd that covers both legs")
    per_tx = getattr(gate, "per_tx_cap_usd", None)
    if isinstance(per_tx, (int, float)) and per_tx > 0 and usd > per_tx:
        return (f"the mint moves about ${usd:,.2f}, above the wallet's limit of ${per_tx:,.2f} "
                f"for one transaction; move a smaller part, or the owner raises the limit")
    daily = getattr(gate, "daily_cap_usd", None)
    if isinstance(daily, (int, float)) and daily > 0:
        spent = float(gate.rolling_24h_spend_usd())
        if spent + usd > daily:
            return (f"the mint moves about ${usd:,.2f}; with ${spent:,.2f} spent in 24 h it "
                    f"passes the wallet's daily limit of ${daily:,.2f}")
    return None


def _mint_report(mint, ref, tid):
    """``(text, ok)`` of a move's mint. ONLY a confirmed receipt is a success: a
    guard refusal and a reverted receipt come back as content, not as an error."""
    text = str(mint.error or mint.extracted_content or "no report returned")
    if not mint.error and "RESULT: success" in text:
        return text, True
    if "tx: 0x" in text:
        return (text + f"\nThe mint was SENT and is not confirmed (or it reverted). Position "
                f"#{tid} holds the remainder and the withdrawn funds are in the wallet until "
                f"the mint lands. Read the transaction and lp_positions(protocol='v4') before "
                f"any new call."), False
    return (text + f"\nThe mint was NOT sent. The withdrawn funds are in the wallet and "
            f"position #{tid} holds the remainder. Run this call again to mint while "
            f"declaration {ref} is CONFIRMED, or declare the range again."), False


async def _perform_move(tool, p, ctx):
    """``lp_add(protocol='v4', seed=<ref>)`` on a MOVE declaration: one
    withdrawal (taken once, by the declaration's own mark), then one mint."""
    from tools.defi import lp_v4 as V, lp_v4_seed
    from core.wallet.broadcast.evm import EvmRail
    from tools.defi.lp_v4_move import stage
    from tools.defi.trade_params import LpRemoveParams
    S = lp_v4_seed.S
    _text = lambda r: str(r.error or r.extracted_content or "no report returned")
    try:
        row = lp_v4_seed.load(p, ctx)
        f = row["fields"]
        wallet = tool._get_wallet()
        if wallet is None:
            from core.wallet.config import WALLET_OFF_TEXT
            raise ValueError(WALLET_OFF_TEXT)
        signer = wallet.operational_signer()
        rpc = getattr(tool, '_lp_rpc', None) or (tool._rail_factory or EvmRail)(
            chain=p.chain, signer=signer)._rpc
        tid = int(f["from_token_id"])
        mark = str(row.get("tx_hash") or "")
        pos = V.position(rpc, p.chain, tid)
        step = stage(f, pos.liquidity, mark)
    except Exception as exc:
        return tool._ar(error=f"refused: {exc}. RESULT: NOT SENT.")
    head = f"move of {f['from_pct']}% of v4 position #{tid} on {p.chain} — declaration {p.seed}\n"

    async def mint(report):
        out = await _perform(tool, p, ctx, "lp_add")
        text, ok = _mint_report(out, p.seed, tid)
        report += "step 2 (mint):\n" + text
        # A dry run that the guard refused is an error too, never a passed quote.
        ok = ok or (p.dry_run and not out.error)
        return tool._ar(content=report) if ok else tool._ar(error=report)

    if step == "mint":
        return await mint(head + f"step 1 (withdraw): done ({mark}) — the position holds the "
                          "remainder\n")
    # The mint is judged FIRST, against what the withdrawal returns: a refusal
    # stops the move before anything leaves the position.
    # The mint OPENS a position: a pause that would refuse it stops the move here,
    # not after the withdrawal (an exit, which the scoped pause lets through).
    facts = {}
    try:
        npm = dex_registry.resolve_position_manager(p.chain, "v4")
        why = _pause_refusal(ctx, entry=True) or _move_precheck(
            tool, p, row, rpc, signer.address, npm, wallet.policy, pos, facts)
    except Exception as exc:
        why = f"the mint could not be checked ({type(exc).__name__}: {exc})"
    if why:
        return tool._ar(error=head + "refused: the mint of the new range would not pass, so "
                        f"nothing was withdrawn: {why}\nRESULT: NOT SENT.")
    remove = LpRemoveParams(chain=p.chain, protocol="v4", token_id=tid,
                            liquidity_pct=int(f["from_pct"]), slippage_bps=int(f["gap_bps"]),
                            max_spend_usd=p.max_spend_usd, dry_run=p.dry_run)
    from tools.defi.lp_v4_verbs import anchor_floor, move_floor
    floor = anchor_floor(f)   # audit 2026-11-21 L-11: the minimums are bound to the anchor
    if p.dry_run:
        with move_floor(tid, floor):
            out = await _perform(tool, remove, ctx, "lp_remove")
        if out.error:
            return tool._ar(error=head + "step 1 (withdraw) would not pass:\n" + _text(out)
                            + "\nRESULT: NOT SENT.")
        return tool._ar(content=head + "step 1 (withdraw):\n" + _text(out) + "\nstep 2 (mint): "
                        "the pool, the gap, the allowances, the LP caps and the USD limits pass "
                        "against what the withdrawal returns at least. The guard simulates the "
                        "mint itself when it is sent, after the withdrawal."
                        + "".join("\n" + n for n in facts.pop("grant_notes", [])), metadata=facts)
    if not S.claim_withdrawal(p.seed):
        return tool._ar(error=head + "refused: this move's withdrawal is already taken by "
                        "another run. It is not sent twice. RESULT: NOT SENT.")
    # An exception here is an unknown outcome: the mark stays, so nothing sends
    # a second withdrawal.
    with move_floor(tid, floor):
        out = await _perform(tool, remove, ctx, "lp_remove")
    text = _text(out)
    sent = re.search(r"tx: (0x[0-9a-fA-F]{64})", text)
    report = head + "step 1 (withdraw):\n" + text + "\n"
    if sent:
        S.set_withdrawal(p.seed, sent.group(1))
    elif "RESULT: NOT SENT" in text or "refused before broadcast" in text \
            or "Nothing was broadcast" in text:
        S.set_withdrawal(p.seed, "")
        return tool._ar(error=report + "Nothing was withdrawn; the move did not start.")
    else:
        return tool._ar(error=report + "The withdrawal's outcome is UNKNOWN. This move is not "
                        "sent again. Read lp_positions(protocol='v4', "
                        f"token_id={tid}) and the wallet's transactions.")
    try:
        landed = stage(f, V.position(rpc, p.chain, tid).liquidity, sent.group(1)) == "mint"
        note = ""
    except Exception as exc:
        landed, note = False, f"{exc}\n"
    if not landed:
        return tool._ar(error=report + note + "step 2 (mint): NOT attempted — the withdrawal "
                        "is not confirmed on the chain. Read lp_positions(protocol='v4', "
                        f"token_id={tid}); when it shows the remainder, run this call again "
                        "(it mints only; it does not withdraw again).")
    return await mint(report)


async def _v3_grants(tool, p, ctx):
    """126 wave 4: the exact approvals a LIVE v3 deposit lacks, made here to the PINNED
    position manager (each judged by the guard). ``(why not, [(token, report)])``."""
    from core.wallet import tx_guard
    from core.wallet.broadcast.evm import EvmRail
    from tools.defi.approve_verb import perform_approve_token
    from tools.defi.trade_params import ApproveParams
    try:
        wallet = tool._get_wallet()
        signer = wallet.operational_signer()
        rpc = getattr(tool, '_lp_rpc', None) or (tool._rail_factory or EvmRail)(
            chain=p.chain, signer=signer)._rpc
        npm = dex_registry.resolve_position_manager(p.chain, 'v3')
        grants = []
        prepare_add(p, rpc, signer.address, npm, price_fn=tool._price, grants=grants)
    except Exception:  # noqa: BLE001 — the send path reports it
        return None, []
    if not grants or npm.lower() not in tx_guard.pinned_route_spenders(p.chain):
        return None, []
    made = []
    for t, n, dec in grants:
        res = await perform_approve_token(tool, ApproveParams(
            chain=p.chain, token=t, spender=npm,
            amount=float(Decimal(n) * Decimal('1.000001') / Decimal(10) ** dec),
            max_spend_usd=p.max_spend_usd, dry_run=False), ctx)
        text = str(res.error or res.extracted_content or '')
        if res.error or 'NOT SENT' in text:
            return f'the exact approval of {t} to {npm} was not made: {text}', made
        made.append((t, text))
    return None, made


async def _revoke_all(tool, p, ctx, spender, made) -> str:
    from tools.defi.approve_verb import perform_revoke_approval
    from tools.defi.trade_params import RevokeParams
    out = []
    for t, _ in made:
        try:
            rev = await perform_revoke_approval(tool, RevokeParams(
                chain=p.chain, token=t, spender=spender, dry_run=False), ctx)
            text = str(rev.error or rev.extracted_content or 'no report')
        except Exception as exc:  # noqa: BLE001
            text = f'raised {exc}'
        out.append(f'revoke leg {t}:\n{text}')
    return '\n'.join(out)


async def perform_lp_add(tool, params, execution_context=None):
    if (getattr(params, 'seed', None) is None and params.protocol == 'v3'
            and not params.dry_run):
        # 126 wave 4: a v3 deposit is ONE call — exact approvals, the mint, the revokes.
        why, made = await _v3_grants(tool, params, execution_context)
        if why:
            # A grant made before the one that failed is revoked here, and the report says
            # so: an exact approval with no deposit behind it is a standing claim.
            undo = ('' if not made else '\n' + await _revoke_all(
                tool, params, execution_context,
                dex_registry.resolve_position_manager(params.chain, 'v3'), made))
            return tool._ar(error=f'refused: {why}\nNothing was deposited. RESULT: NOT SENT.'
                                  + undo)
        res = await _perform(tool, params, execution_context, 'lp_add')
        if not made:
            return res
        legs = ('\n'.join(f'approve leg {t}:\n{x}' for t, x in made) + '\n'
                + await _revoke_all(tool, params, execution_context,
                                    dex_registry.resolve_position_manager(params.chain, 'v3'),
                                    made))
        if res.error:
            return tool._ar(error=str(res.error) + '\n' + legs)
        return tool._ar(content=str(res.extracted_content or '') + '\n' + legs,
                        metadata=getattr(res, 'metadata', None) or {})
    if getattr(params, 'seed', None) is not None:
        # 118 W7: a declaration that MOVES a position is two guarded transactions.
        from tools.defi import lp_v4_seed
        if not params.dry_run:
            # The Permit2 legs of a confirmed declaration are made here, never asked for.
            why, made = await lp_v4_seed.make_grants(tool, params, execution_context)
            if why:
                # The grants made before the one that failed stay for a retry of this
                # declaration (each is sized to its mint; a Permit2 grant expires), and the
                # report names them: the ERC-20 allowance to Permit2 has no expiry.
                kept = ("" if not made else "\nThese grants were made and STAY until this "
                        "declaration is retried or revoke_approval removes them:\n"
                        + "\n".join(made))
                return tool._ar(error=f"refused: {why}\nNothing was withdrawn or minted. "
                                      "RESULT: NOT SENT." + kept)
        if lp_v4_seed.is_move(params, execution_context):
            return await _perform_move(tool, params, execution_context)
    return await _perform(tool, params, execution_context, 'lp_add')


async def perform_lp_remove(tool, params, execution_context=None):
    return await _perform(tool, params, execution_context, 'lp_remove')


async def perform_lp_collect(tool, params, execution_context=None):
    return await _perform(tool, params, execution_context, 'lp_collect')
