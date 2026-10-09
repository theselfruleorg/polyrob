"""Liquidity builders and lifecycle: calldata, ownership, receipts and accounting."""
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, simulation
from core.wallet.tx_guard import Decision
from tools.defi import lp_abi as A, lp_reads as R, lp_verbs as V
from tools.defi.trade_tool import DefiTradeTool, LpAddParams, LpCollectParams, LpRemoveParams
from tests.unit.tools.defi.test_deploy_and_call_verbs import _Gate, _Wallet, _Rail, HOLDER

T0, T1 = '0x' + '1' * 40, '0x' + '9' * 40
NPM = dex_registry.row_for('base', 'v3').position_manager
POOL = '0x' + '7' * 40
ZERO = '0x' + '0' * 40


def add(**kw):
    return LpAddParams(**(dict(chain='base', token_a=T0, token_b=T1,
        amount_a=1, amount_b=1, max_spend_usd=5, initial_price=1) | kw))


@pytest.fixture(autouse=True)
def reads(monkeypatch):
    monkeypatch.setenv(V.FLAG, 'true')
    monkeypatch.setattr(V, '_pons_warning', lambda *a: '')
    monkeypatch.setattr(dex_registry, 'verify_pins', lambda *a: None)
    monkeypatch.setattr(R, 'pool_address', lambda *a: None)
    monkeypatch.setattr(R, 'collectible_fees', lambda *a: (3, 4))
    monkeypatch.setattr(R, 'allowance', lambda *a: 10**30)
    monkeypatch.setattr(R, 'view', lambda rpc, to, spec, args=None:
        HOLDER if spec['name'] == 'ownerOf' else 18 if spec['name'] == 'decimals' else 10**30)
    monkeypatch.setattr(R, 'position', lambda *a: R.PositionView(
        42, POOL, T0, T1, 3000, -887220, 887220, 10**18, 10**18, 10**18, 3, 4, True, 18, 18))
    monkeypatch.setattr(R, 'pool_state', lambda *a: SimpleNamespace(sqrt_price_x96=2**96))


def event(topic, token_id, data):
    return dict(address=NPM, topics=[topic, hex(token_id)], data=data)


def mint_receipt(token_id=99):
    transfers = [dict(address=t, topics=[simulation._TOPIC_TRANSFER,
        '0x' + HOLDER[2:].rjust(64, '0'), '0x' + POOL[2:].rjust(64, '0')],
        data='0x' + (10**18).to_bytes(32, 'big').hex()) for t in (T0, T1)]
    return {'logs': transfers + [dict(address=NPM, topics=[simulation._TOPIC_TRANSFER,
        '0x' + '0' * 64, '0x' + HOLDER[2:].rjust(64, '0'), hex(token_id)], data='0x'),
        event(A.TOPIC_INCREASE_LIQUIDITY, token_id, '0x' + abi.encode(
            [{'type': 'uint128'}, {'type': 'uint256'}, {'type': 'uint256'}],
            [10**18, 10**18, 10**18]).hex())]}


def tool(captured=None, receipt=None, allow=True):
    gate = _Gate()
    def guard(i, tx, **kw):
        if captured is not None:
            captured.append(i)
        return Decision(allow, 'test', 'autonomous', 2, 400000, position_token_id=42)
    t = DefiTradeTool(wallet=_Wallet(gate), rail_factory=_Rail, guard_fn=guard,
        price_fn=lambda *a: 1, fallback_price_fn=lambda *a: None)
    t._lp_rpc = lambda *a: receipt or mint_receipt()
    t._notify_tx = lambda *a, **kw: None
    return t, gate


@pytest.mark.asyncio
@pytest.mark.parametrize('verb,params', [('lp_add', add()), ('lp_remove', LpRemoveParams(token_id=42)),
                                        ('lp_collect', LpCollectParams(token_id=42))])
async def test_flag_and_leaf(monkeypatch, verb, params):
    t, _ = tool()
    monkeypatch.delenv(V.FLAG)
    assert V.FLAG in (await getattr(t, verb)(params)).error
    monkeypatch.setenv(V.FLAG, 'true')
    result = await getattr(t, verb)(params, SimpleNamespace(is_sub_agent=True, role='leaf'))
    assert 'sub-agent' in result.error


@pytest.mark.asyncio
async def test_dry_run_and_new_pool_intent():
    captured = []
    t, gate = tool(captured)
    result = await t.lp_add(add())
    assert 'DRY RUN' in result.extracted_content, result.error
    assert 'would mint position #42' in result.extracted_content
    assert not _Rail.last.sent and not gate.recorded
    i = captured[0]
    assert i.is_liquidity_op and len(i.lp_outflows) == 2
    assert (dex_registry.row_for('base', 'v3').factory, A.TOPIC_POOL_CREATED) in i.expected_events
    assert _Rail.last.built['data'].startswith('0xac9650d8')


@pytest.mark.asyncio
async def test_retry_key_survives_refreshed_lp_deadline(monkeypatch):
    captured = []
    t, _ = tool(captured)
    monkeypatch.setattr(V.time, 'time', lambda: 1000)
    await t.lp_add(add())
    first_data = _Rail.last.built['data']
    monkeypatch.setattr(V.time, 'time', lambda: 1005)
    await t.lp_add(add())
    assert first_data != _Rail.last.built['data']
    assert len(captured) == 2
    assert captured[0].idempotency_key == captured[1].idempotency_key
    assert captured[0].idempotency_key



@pytest.mark.asyncio
async def test_missing_price_and_allowance_remedy(monkeypatch):
    t, _ = tool()
    assert 'initial_price' in (await t.lp_add(add(initial_price=None))).error
    monkeypatch.setattr(R, 'allowance', lambda *a: 0)
    res = await t.lp_add(add())
    assert 'approve_token' in res.error and NPM in res.error
    # Our own measured shortfall: a precondition, never a refusal taint.
    assert (res.metadata or {}).get('error_kind') == 'precondition'


@pytest.mark.asyncio
async def test_insufficient_balance_is_a_precondition_not_a_taint(monkeypatch):
    t, _ = tool()
    monkeypatch.setattr(R, 'view', lambda rpc, to, spec, args=None:
        HOLDER if spec['name'] == 'ownerOf' else 18 if spec['name'] == 'decimals' else 1)
    res = await t.lp_add(add())
    assert 'insufficient balance' in res.error
    assert (res.metadata or {}).get('error_kind') == 'precondition'


@pytest.mark.asyncio
async def test_an_unexpected_build_failure_still_taints(monkeypatch):
    t, _ = tool()
    monkeypatch.setattr(R, 'allowance', lambda *a: (_ for _ in ()).throw(ValueError('boom')))
    res = await t.lp_add(add())
    assert 'boom' in res.error
    assert (res.metadata or {}).get('error_kind') is None


@pytest.mark.asyncio
async def test_v4_builds_a_full_range_mint_only():
    # 048 phase 3 (core handoff W6): v4 exits are not built (090 R5).
    t, _ = tool()
    assert 'lp_add' in (await t.lp_remove(LpRemoveParams(token_id=42, protocol='v4'))).error
    assert 'lp_add' in (await t.lp_collect(LpCollectParams(token_id=42, protocol='v4'))).error
    assert 'unknown protocol' in (await t.lp_add(add(protocol='v5'))).error


def _v4_reads(monkeypatch, t):
    from tools.defi import lp_v4 as V4
    key = R.pons_pool_key({'token': T1, 'pairToken': ZERO, 'poolFee': 0, 'tickSpacing': 200})
    monkeypatch.setattr(V4, 'resolve_pons_pair', lambda rpc, a, b: (key, R.pool_id(key), False))
    monkeypatch.setattr(dex_registry, 'verify_hook', lambda *a: None)
    monkeypatch.setattr(V4, 'pool_state', lambda *a: V4.V4PoolState(
        R.pool_id(key), 2 ** 96, 0, 0, 0, 10 ** 22, 'state_view'))
    monkeypatch.setattr(V4, 'permit2_allowance', lambda *a: (10 ** 30, 2 ** 40))
    t._lp_rpc = lambda m, p: hex(10 ** 20) if m == 'eth_getBalance' else mint_receipt()
    t._fallback_price = lambda *a: None
    t._price = lambda *a: None
    return key


def v4add(**kw):
    return add(**(dict(chain='robinhood', protocol='v4', token_a='native', token_b=T1,
                       amount_a=0.01, amount_b=1000, initial_price=None) | kw))


@pytest.mark.asyncio
async def test_v4_dry_run_intent_is_the_guarded_mint(monkeypatch):
    captured = []
    t, gate = tool(captured)
    _v4_reads(monkeypatch, t)
    res = await t.lp_add(v4add())
    assert 'DRY RUN' in res.extracted_content, res.error
    assert 'LP caps: LP_ETH_CAP is 0' in res.extracted_content
    i = captured[0]
    row = dex_registry.row_for('robinhood', 'v4')
    assert i.to == row.position_manager and i.lp_position == (row.position_manager, None)
    assert i.watch_spenders == (row.permit2,)
    from tools.defi.lp_v4 import TOPIC_MODIFY_LIQUIDITY
    assert i.expected_events == ((row.pool_manager, TOPIC_MODIFY_LIQUIDITY),)
    assert dict(i.lp_outflows)[None] == _Rail.last.built['value']
    assert _Rail.last.built['data'].startswith(abi.selector('modifyLiquidities(bytes,uint256)'))
    from core.wallet import liquidity_guard
    liquidity_guard.structural(i, dict(_Rail.last.built, chainId=4663))


@pytest.mark.asyncio
async def test_v4_caps_refuse_before_the_guard(monkeypatch):
    captured = []
    t, gate = tool(captured)
    gate.audit_log = []
    gate._sync_shared_ledger = lambda: True
    _v4_reads(monkeypatch, t)
    monkeypatch.delenv('LP_ETH_CAP', raising=False)
    t._price = lambda *a: 1.0
    res = await t.lp_add(v4add(dry_run=False))
    assert 'LP_ETH_CAP is 0' in res.error and not captured and not gate.recorded


@pytest.mark.asyncio
async def test_v4_missing_permit2_grant_names_the_remedy(monkeypatch):
    from tools.defi import lp_v4 as V4
    t, _ = tool()
    _v4_reads(monkeypatch, t)
    monkeypatch.setattr(V4, 'permit2_allowance', lambda *a: (0, 0))
    res = await t.lp_add(v4add())
    assert "via='permit2'" in res.error
    assert (res.metadata or {}).get('error_kind') == 'precondition'


def test_increase_retains_range_and_checks_pair(monkeypatch):
    monkeypatch.setattr(R, 'pool_address', lambda *a: POOL)
    plan = V.prepare_add(add(token_id=42), lambda *a: None, HOLDER, NPM)
    assert plan.intent.lp_position_effect == 'hold'
    assert plan.intent.lp_position[1] == 42
    with pytest.raises(ValueError, match='pair/fee'):
        V.prepare_add(add(token_id=42, fee=500), lambda *a: None, HOLDER, NPM)


def test_native_leg_and_refund():
    plan = V.prepare_add(add(token_a='native'), lambda *a: hex(10**30), HOLDER, NPM)
    assert plan.value > 0
    assert any(t is None for t, _ in plan.intent.lp_outflows)
    assert bytes.fromhex(V.encode(A.NPM_REFUND_ETH)[2:]) in bytes.fromhex(plan.data[2:])


def test_remove_collect_and_burn():
    plan = V.prepare_exit(LpRemoveParams(chain='base', token_id=42, burn=True), None, HOLDER, NPM, 'lp_remove')
    assert plan.intent.lp_position_effect == 'burn' and len(plan.intent.lp_inflows) == 2
    assert (NPM, A.TOPIC_DECREASE_LIQUIDITY) in plan.intent.expected_events
    with pytest.raises(ValueError, match='100'):
        V.prepare_exit(LpRemoveParams(token_id=42, burn=True, liquidity_pct=50), None, HOLDER, NPM, 'lp_remove')
    plan = V.prepare_exit(LpCollectParams(chain='base', token_id=42), None, HOLDER, NPM, 'lp_collect')
    assert plan.intent.lp_inflows == ((T0, 2), (T1, 3))
    assert not plan.intent.lp_outflows


@pytest.mark.asyncio
async def test_landed_id_recorded_without_double_counting_underlying_tokens(monkeypatch):
    monkeypatch.setattr(R, 'pool_address', lambda *a: POOL)
    t, gate = tool()
    result = await t.lp_add(add(dry_run=False))
    assert _Rail.last.sent
    assert 'position #99' in result.extracted_content
    assert gate.recorded[0]['asset'] == f'erc721:{NPM.lower()}:99'
    assert gate.recorded[0]['positions'] == []


@pytest.mark.asyncio
async def test_missing_receipt_still_books_cap_without_phantom_position(monkeypatch):
    monkeypatch.setattr(R, 'pool_address', lambda *a: POOL)
    t, gate = tool(receipt={'logs': []})
    result = await t.lp_add(add(dry_run=False))
    assert 'unverified' in result.extracted_content
    assert gate.recorded[0]['amount_usd'] == 2
    assert gate.recorded[0]['asset'] is None
    assert not gate.recorded[0]['positions']


@pytest.mark.asyncio
async def test_guard_refusal_never_sends():
    t, gate = tool(allow=False)
    result = await t.lp_add(add(dry_run=False))
    assert 'NOT SENT' in result.extracted_content
    assert not _Rail.last.sent and not gate.recorded


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0.0000001'])
def test_raw_amount_never_rounds_or_accepts_nonfinite(value):
    with pytest.raises(ValueError):
        V.raw_amount(value, 6)


def test_collect_minimum_uses_actual_small_balance(monkeypatch):
    monkeypatch.setattr(R, 'collectible_fees', lambda *a: (270, 0))
    plan = V.prepare_exit(LpCollectParams(chain='base', token_id=42), None, HOLDER, NPM, 'lp_collect')
    assert plan.intent.lp_inflows == ((T0, 264), (T1, 0))


@pytest.mark.asyncio
async def test_landed_transfer_shortfall_books_cap_but_not_position(monkeypatch):
    monkeypatch.setattr(R, 'pool_address', lambda *a: POOL)
    receipt = mint_receipt()
    receipt['logs'] = receipt['logs'][1:]
    t, gate = tool(receipt=receipt)
    result = await t.lp_add(add(dry_run=False))
    assert 'unverified' in result.extracted_content
    assert gate.recorded[0]['amount_usd'] == 2
    assert gate.recorded[0]['asset'] is None
    assert gate.recorded[0]['positions'] == []


def test_every_v3_plan_passes_the_guard_calldata_decode(monkeypatch):
    """WAL-2: the guard decodes the signed v3 calldata. Every shape the verbs
    build (new pool, native leg, increase, remove+burn, fee collection) must
    pass it, bound to the wallet, and a foreign recipient must not."""
    from core.wallet import liquidity_guard
    plans = [V.prepare_add(add(), lambda *a: None, HOLDER, NPM),
             V.prepare_add(add(token_a='native'), lambda *a: hex(10**30), HOLDER, NPM),
             V.prepare_exit(LpRemoveParams(chain='base', token_id=42, burn=True), None, HOLDER, NPM, 'lp_remove'),
             V.prepare_exit(LpRemoveParams(chain='base', token_id=42), None, HOLDER, NPM, 'lp_remove'),
             V.prepare_exit(LpCollectParams(chain='base', token_id=42), None, HOLDER, NPM, 'lp_collect')]
    monkeypatch.setattr(R, 'pool_address', lambda *a: POOL)
    plans.append(V.prepare_add(add(token_id=42), lambda *a: None, HOLDER, NPM))
    for plan in plans:
        tx = dict(to=NPM, value=plan.value, data=plan.data)
        liquidity_guard.structural(plan.intent, tx, holder=HOLDER)
        if plan.intent.lp_position_effect == 'hold' and plan.intent.lp_outflows:
            continue   # increaseLiquidity names no recipient; the position is already ours
        with pytest.raises(ValueError):
            liquidity_guard.structural(plan.intent, tx, holder='0x' + '6' * 40)
