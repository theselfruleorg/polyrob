"""Exercise liquidity through the real authorizer, including adverse deltas."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.wallet import abi, dex_registry, tx_guard as G
from eth_utils import keccak
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

NPM = dex_registry.row_for('base', 'v3').position_manager.lower()
A, B, C = ('0x' + c * 40 for c in ('1', '2', '3'))
ZERO = '0x' + '0' * 40
HOLDER = '0x' + '4' * 40
EVENT = '0x' + '5' * 64


def intent(**kw):
    base = dict(chain='base', token=None, to=NPM, amount_raw=0,
                max_spend_usd=50, is_liquidity_op=True,
                lp_outflows=((A, 10**6), (B, 10**6)),
                lp_position=(NPM, None), lp_position_effect='mint')
    return G.TxIntent(**(base | kw))


def deltas(**kw):
    signatures = ['IncreaseLiquidity(uint256,uint128,uint256,uint256)']
    if kw.get('holder_nft_in') == ():
        signatures = ['Collect(uint256,address,uint256,uint256)', 'DecreaseLiquidity(uint256,uint128,uint256,uint256)']
    logs = tuple(dict(address=NPM, topics=['0x' + keccak(text=sig).hex(), hex(42)],
                      data='0x' + ('0' * 63 + '1') * 3) for sig in signatures)
    kw.setdefault('logs', logs)
    return Deltas(**(dict(ok=True, token_deltas={A: -10**6, B: -10**6},
        holder_nft_in=((NPM, 'erc721', ZERO, 42, 1),), gas_used=300000) | kw))


@pytest.fixture(autouse=True)
def probes(monkeypatch):
    monkeypatch.setattr(dex_registry, 'verify_pins', lambda *a: None)
    monkeypatch.setattr(G, '_decimals_for', lambda *a: 6)


def _call(name, *args):
    from core.wallet.liquidity_guard import _V3_CALLS
    return abi.encode_call(name, _V3_CALLS[name], list(args))


def v3_data(i, recipient=HOLDER, decrease_min=(1, 1)):
    """The calldata the v3 verbs build for intent *i* (WAL-2: the guard decodes it)."""
    tid = i.lp_position[1]
    if i.lp_outflows:
        if tid is None:
            calls = [_call('mint', (A, B, 3000, -60, 60, 1, 1, 0, 0, recipient, 9))]
        else:
            calls = [_call('increaseLiquidity', (tid, 1, 1, 0, 0, 9))]
    else:
        calls = [_call('decreaseLiquidity', (tid, 5, *decrease_min, 9)),
                 _call('collect', (tid, recipient, 2**128 - 1, 2**128 - 1))]
        if i.lp_position_effect == 'burn':
            calls.append(_call('burn', tid))
    if len(calls) == 1:
        return calls[0]
    return abi.encode_call('multicall', [{'type': 'bytes[]'}],
                           [[bytes.fromhex(c[2:]) for c in calls]])


def run(i=None, d=None, **kw):
    i = i or intent()
    tx = kw.pop('tx', None)
    if tx is None:
        try:
            data = v3_data(i)
        except Exception:
            data = '0x'
        tx = dict(to=NPM, value=0, chainId=8453, maxFeePerGas=10**9, data=data)
    return G.authorize(i, tx, holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=100, daily_cap_usd=1000),
        simulate_fn=lambda **args: d or deltas(),
        liquidity_rpc=lambda *a: abi.encode([{'type': 'address'}], [HOLDER]),
        **(dict(price_fn=lambda *a: 1.0, halted_fn=lambda: False,
                entry_paused_fn=lambda: False, rpc_is_pinned_fn=lambda c: True) | kw))


def test_two_leg_mint():
    result = run()
    assert result.allowed, result.reason
    assert result.position_token_id == 42
    assert result.amount_usd == 2.01


@pytest.mark.parametrize('change', [
    dict(token_deltas={A: -10**6, B: -10**6, C: -1}),
    dict(holder_transfers=((C, NPM, 1),)),
    dict(holder_nft_in=()),
    dict(holder_nft_in=((C, 'erc721', ZERO, 42, 1),)),
    dict(holder_nft_in=((NPM, 'erc721', ZERO, 42, 1), (NPM, 'erc721', ZERO, 43, 1))),
    dict(holder_nft_in=((NPM, 'erc721', HOLDER, 42, 1),)),
    dict(holder_nft_out=((C, 'erc721', NPM, 1, 1),)),
    dict(token_deltas={A: -10**6, B: 0}),
    dict(token_deltas={A: -10**6 - 1, B: -10**6}),
    dict(native_delta=-10**15),
    dict(holder_operator_grants=((NPM, C, True),)),
    dict(holder_nft_approvals=((NPM, C, 42),)),
    dict(allowance_deltas={(A, C): 1}),
])
def test_adverse_simulation_refuses(change):
    assert not run(d=deltas(**change)).allowed


@pytest.mark.parametrize('change', [
    dict(is_claim=True), dict(is_deploy=True), dict(is_nft_op=True),
    dict(is_registration=True), dict(is_allowance_op=True),
    dict(to=C), dict(lp_position=(C, None)), dict(lp_position=(NPM, 42)),
    dict(lp_outflows=((A, 1), (A.upper().replace('0X', '0x'), 1))),
    dict(lp_outflows=((A, 1), (B, 1), (C, 1))),
    dict(lp_outflows=((A, 0),)), dict(lp_inflows=((A, 1),)),
    dict(lp_outflows=(), lp_inflows=(), expected_events=()),
    dict(expected_events=((C, EVENT),)),
])
def test_invalid_declaration_refuses(change):
    assert not run(intent(**change)).allowed


def test_destination_must_match_signed_tx():
    assert not run(tx=dict(to=C, value=0)).allowed


def test_native_leg_matches_value():
    i = intent(lp_outflows=((None, 10**15), (A, 10**6)))
    d = deltas(native_delta=-10**15, token_deltas={A: -10**6})
    assert not run(i, d).allowed
    assert run(i, d, tx=dict(to=NPM, value=10**15, data=v3_data(i))).allowed


def test_required_event_from_exact_emitter():
    i = intent(expected_events=((NPM, EVENT),))
    assert not run(i).allowed
    assert not run(i, deltas(event_topics=((C, EVENT),))).allowed
    assert run(i, deltas(event_topics=((NPM, EVENT),))).allowed


def test_implied_price_and_two_unpriceable():
    # CR-L13: the paired-leg valuation holds only for a pool THIS tx creates.
    created_event = (dex_registry.row_for('base', 'v3').factory,
                     '0x' + keccak(text='PoolCreated(address,address,uint24,int24,address)').hex())
    d = run(intent(expected_events=(created_event,)),
            deltas(event_topics=(created_event,)),
            price_fn=lambda chain, token: 1 if token != B else None)
    assert d.allowed and d.amount_usd == 2.01 and 'implied' in d.reason
    assert not run(price_fn=lambda *a: None).allowed
    assert not run(price_fn=lambda *a: float('nan')).allowed


def test_hold_and_collect_assert_each_receipt():
    i = intent(lp_outflows=(), lp_inflows=((A, 10), (B, 20)),
               lp_position=(NPM, 42), lp_position_effect='hold')
    d = deltas(token_deltas={A: 10, B: 20}, holder_nft_in=())
    result = run(i, d, price_fn=lambda *a: 3000)
    assert result.allowed, result.reason
    assert result.amount_usd > 0
    assert not run(i, replace(d, token_deltas={A: 10, B: 0})).allowed
    assert not run(i, replace(d, holder_nft_out=((NPM, 'erc721', C, 42, 1),))).allowed


def test_burn_requires_exact_zero_recipient_and_allows_approval_clear():
    i = intent(lp_outflows=(), lp_inflows=((A, 10), (B, 20)),
               lp_position=(NPM, 42), lp_position_effect='burn')
    d = deltas(token_deltas={A: 10, B: 20}, holder_nft_in=(),
        holder_nft_out=((NPM, 'erc721', ZERO, 42, 1),),
        holder_nft_approvals=((NPM, ZERO, 42),))
    assert run(i, d).allowed
    assert not run(i, replace(d, holder_nft_out=((NPM, 'erc721', C, 42, 1),))).allowed
    assert not run(replace(i, lp_inflows=(), expected_events=((NPM, EVENT),)), d).allowed


def test_pause_and_forged_leaf():
    assert not run(halted_fn=lambda: True).allowed
    ctx = SimpleNamespace(is_sub_agent=True, role='leaf')
    assert not run(execution_context=ctx, forged_fn=lambda *a: True).allowed


def test_pin_failure_refuses(monkeypatch):
    def fail(*a):
        raise ValueError('code hash mismatch')
    monkeypatch.setattr(dex_registry, 'verify_pins', fail)
    assert 'code hash mismatch' in run().reason


def test_deposit_cannot_fund_somebody_elses_position():
    d = deltas(holder_nft_in=())
    wrong_log = dict(address=NPM, topics=['0x' + keccak(text='IncreaseLiquidity(uint256,uint128,uint256,uint256)').hex(), hex(999)],
                     data='0x' + ('0' * 63 + '1') * 3)
    i = intent(lp_position=(NPM, 42), lp_position_effect='hold')
    result = run(i, replace(d, logs=(wrong_log,)))
    assert not result.allowed and 'different position' in result.reason


def test_cr_l13_existing_pool_refuses_the_paired_leg_valuation():
    """In an existing pool the ratio is whatever its last trader seeded, so the
    unpriceable leg may not borrow the priced leg's USD."""
    d = run(price_fn=lambda chain, token: 1 if token != B else None)
    assert not d.allowed and 'already exists' in d.reason


# -- WAL-2: the signed v3 calldata is bound, not only the simulated deltas ----

def _collect_intent(**kw):
    return intent(**(dict(lp_outflows=(), lp_inflows=((A, 10), (B, 20)),
                          lp_position=(NPM, 42), lp_position_effect='hold') | kw))


def _collect_deltas():
    return deltas(token_deltas={A: 10, B: 20}, holder_nft_in=())


def _tx(data, value=0):
    return dict(to=NPM, value=value, chainId=8453, data=data)


def test_collect_to_another_recipient_refuses_before_simulation():
    i = _collect_intent()
    result = run(i, _collect_deltas(), tx=_tx(v3_data(i, recipient=C)), price_fn=lambda *a: 1)
    assert not result.allowed and 'collect pays' in result.reason


def test_fee_only_withdrawal_needs_a_positive_minimum():
    i = _collect_intent(lp_inflows=((A, 0), (B, 0)))
    result = run(i, _collect_deltas(), price_fn=lambda *a: 1)
    assert not result.allowed and 'positive minimum' in result.reason


def test_one_sided_fee_collection_still_passes():
    i = _collect_intent(lp_inflows=((A, 10), (B, 0)))
    d = deltas(token_deltas={A: 10, B: 0}, holder_nft_in=())
    assert run(i, d, price_fn=lambda *a: 1).allowed


def test_decrease_without_an_onchain_floor_refuses():
    i = _collect_intent()
    result = run(i, _collect_deltas(), tx=_tx(v3_data(i, decrease_min=(0, 0))),
                 price_fn=lambda *a: 1)
    assert not result.allowed and 'positive on-chain minimum' in result.reason


def test_other_position_or_foreign_call_refuses():
    i = _collect_intent()
    other = _call('collect', (43, HOLDER, 1, 1))
    assert 'not the declared' in run(i, _collect_deltas(), tx=_tx(other)).reason
    transfer = abi.encode_call('safeTransferFrom', [{'type': 'address'}] * 2 + [{'type': 'uint256'}],
                               [HOLDER, C, 42])
    assert 'no liquidity verb builds' in run(i, _collect_deltas(), tx=_tx(transfer)).reason
    assert not run(i, _collect_deltas(), tx=_tx('0x')).allowed


def test_mint_to_another_recipient_refuses():
    i = intent()
    assert 'mint the position to the wallet' in run(i, tx=_tx(v3_data(i, recipient=C))).reason


def test_deposit_may_not_carry_a_withdrawal():
    i = intent()
    data = abi.encode_call('multicall', [{'type': 'bytes[]'}], [[
        bytes.fromhex(v3_data(i)[2:]),
        bytes.fromhex(_call('collect', (42, HOLDER, 1, 1))[2:])]])
    assert 'may not call' in run(i, tx=_tx(data)).reason
