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


def run(i=None, d=None, **kw):
    tx = kw.pop('tx', dict(to=NPM, value=0, chainId=8453, maxFeePerGas=10**9))
    return G.authorize(i or intent(), tx, holder=HOLDER,
        gate=PolicyGate(max_per_tx_usd=100, daily_cap_usd=1000),
        simulate_fn=lambda **args: d or deltas(),
        liquidity_rpc=lambda *a: abi.encode([{'type': 'address'}], [HOLDER]),
        **(dict(price_fn=lambda *a: 1.0, halted_fn=lambda: False,
                entry_paused_fn=lambda: False, rpc_is_pinned_fn=lambda c: True) | kw))


def test_two_leg_mint():
    result = run()
    assert result.allowed, result.reason
    assert result.position_token_id == 42
    assert result.amount_usd == 2


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
    assert run(i, d, tx=dict(to=NPM, value=10**15)).allowed


def test_required_event_from_exact_emitter():
    i = intent(expected_events=((NPM, EVENT),))
    assert not run(i).allowed
    assert not run(i, deltas(event_topics=((C, EVENT),))).allowed
    assert run(i, deltas(event_topics=((NPM, EVENT),))).allowed


def test_implied_price_and_two_unpriceable():
    d = run(price_fn=lambda chain, token: 1 if token == A else None)
    assert d.allowed and d.amount_usd == 2 and 'implied' in d.reason
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
