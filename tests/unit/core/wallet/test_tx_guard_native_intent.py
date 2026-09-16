"""tx_guard — a NATIVE-value transaction (039 Unit B1).

Before this, ``TxIntent.token=None`` had no meaning the guard could act on: the
outflow branch is keyed on ``intent.token``, so a native send skipped it entirely
and then hit step 6's blanket

    if abs(deltas.native_delta) > _NATIVE_DUST_WEI: refuse

whose own comment says it exists because "native balance is not expected to move
on an ERC-20 transfer". The guard was ERC-20-only by construction, which is the
real reason ``bridge_verb.py`` carried a bare "an EVM-origin bridge is not wired
yet" — a Relay EVM deposit IS a native-value call.

The assertions below mirror the Solana branch in ``bridge_verb``, where fee and
principal are also one asset: an inflow refuses, a ZERO delta is a measurement
failure rather than a free transaction, a materially short move is not the
transaction that was priced, and an excess beyond dust is undeclared.
"""
import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

# Relay's depository on Base — an ordinary contract; the point is that a native
# send goes TO a contract, which is why "to" being a contract must not refuse.
RELAY = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HOLDER = "0x2222222222222222222222222222222222222222"
WETH_BASE = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

ONE_ETH = 10 ** 18
#: 0.005 ETH. Deliberately UNDER the $25 autonomous ceiling at the price below, so
#: the happy-path test proves `allowed`, not `lane="owner_queue"`. The ceiling is
#: exercised on purpose in its own test further down.
AMOUNT = 5 * 10 ** 15
#: Price used throughout so the USD maths is checkable by hand: 0.005 * 2000 = $10.
ETH_PRICE = 2000.0


def _intent(**kw):
    base = dict(chain="base", token=None, to=RELAY, amount_raw=AMOUNT,
                max_spend_usd=100.0, expected_allowance_grants=(),
                idempotency_key="native-1")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _deltas(native_delta=-AMOUNT, **kw):
    base = dict(ok=True, native_delta=native_delta, token_deltas={},
                allowance_deltas={}, gas_used=90_000)
    base.update(kw)
    return Deltas(**base)


def _gate():
    return PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0)


def _authorize(intent=None, deltas=None, *, price=ETH_PRICE, gate=None):
    """A native-value tx: ``value`` is non-zero and ``data`` carries the order."""
    return tx_guard.authorize(
        intent or _intent(),
        {"to": RELAY, "data": "0xdeadbeef", "value": AMOUNT, "chainId": 8453},
        holder=HOLDER,
        gate=gate or _gate(),
        execution_context=None,
        simulate_fn=lambda **_: deltas if deltas is not None else _deltas(),
        price_fn=lambda chain, addr: price,
        fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=lambda: False,
        forged_fn=lambda ctx, tool: False,
    )


# --------------------------------------------------------------------------
# The happy path — which is the whole point: it did not exist before.
# --------------------------------------------------------------------------

def test_native_send_matching_the_declaration_is_authorized():
    d = _authorize()
    assert d.allowed is True, d.reason
    # 0.005 ETH at $2000 = $10, priced through the chain's wrapped native.
    assert d.amount_usd == pytest.approx(10.0)
    assert d.sim_gas_used == 90_000


def test_native_send_is_priced_through_the_chains_wrapped_native():
    """The guard must price the outflow ITSELF, never trust a caller's figure.

    WETH is the pricing proxy for ETH — same asset, same number — and it is what
    the chain registry pins.
    """
    seen = []

    def _price(chain, addr):
        seen.append((chain, addr))
        return ETH_PRICE

    tx_guard.authorize(
        _intent(), {"to": RELAY, "data": "0x", "value": AMOUNT, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: _deltas(),
        price_fn=_price, fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)
    assert ("base", WETH_BASE) in seen


# --------------------------------------------------------------------------
# Every way the measurement can disagree with the declaration
# --------------------------------------------------------------------------

def test_native_inflow_for_a_send_refuses():
    d = _authorize(deltas=_deltas(native_delta=+AMOUNT))
    assert d.allowed is False
    assert "inflow" in d.reason.lower()


def test_zero_native_delta_is_a_measurement_failure_not_a_free_transaction():
    """The 2026-08-09 lesson, restated for native.

    A 0.25 USDC transfer once broadcast live because eth_call does not persist
    state, every delta read back 0, and the outflow priced at $0.00. Zero is never
    a cheap transaction — it is an unmeasured one.
    """
    d = _authorize(deltas=_deltas(native_delta=0))
    assert d.allowed is False
    assert "no outflow" in d.reason.lower() or "measurement" in d.reason.lower()


def test_native_send_moving_materially_less_than_declared_refuses():
    """A bridge that sends less than it quoted is not the order that was priced —
    and the arrival floor was computed from the quoted amount."""
    d = _authorize(deltas=_deltas(native_delta=-(AMOUNT // 2)))
    assert d.allowed is False
    assert "materially less" in d.reason.lower()


def test_native_send_moving_more_than_declared_refuses():
    d = _authorize(deltas=_deltas(native_delta=-(AMOUNT * 2)))
    assert d.allowed is False
    assert "exceeds" in d.reason.lower() or "more" in d.reason.lower()


def test_dust_sized_excess_is_tolerated():
    """Fee and principal are the same asset on a native move, so an exact match
    cannot be demanded. The band is the SAME pinned dust constant the ERC-20
    branch uses, not a new number."""
    d = _authorize(deltas=_deltas(native_delta=-(AMOUNT + tx_guard._NATIVE_DUST_WEI // 2)))
    assert d.allowed is True, d.reason


def test_dust_sized_shortfall_is_tolerated():
    d = _authorize(deltas=_deltas(native_delta=-(AMOUNT - tx_guard._NATIVE_DUST_WEI // 2)))
    assert d.allowed is True, d.reason


# --------------------------------------------------------------------------
# Everything the ERC-20 path already refuses must still refuse
# --------------------------------------------------------------------------

def test_undeclared_allowance_grant_still_refuses_on_a_native_send():
    d = _authorize(deltas=_deltas(allowance_deltas={(USDC, RELAY): 10 ** 9}))
    assert d.allowed is False
    assert "undeclared" in d.reason.lower()


def test_undeclared_token_transfer_still_refuses_on_a_native_send():
    """A native bridge deposit moves ETH and nothing else. An ERC-20 Transfer out
    of the wallet in the same transaction is an asset the intent never mentioned."""
    d = _authorize(deltas=_deltas(holder_transfers=((USDC, RELAY, 500_000),)))
    assert d.allowed is False
    assert "undeclared" in d.reason.lower()


def test_untrustworthy_simulation_still_refuses():
    d = _authorize(deltas=Deltas(ok=False, error="reverted"))
    assert d.allowed is False
    assert "simulation" in d.reason.lower()


def test_zero_amount_native_send_refuses():
    d = _authorize(intent=_intent(amount_raw=0))
    assert d.allowed is False


def test_burn_address_still_refuses_for_a_native_send():
    d = _authorize(intent=_intent(to="0x000000000000000000000000000000000000dEaD"))
    assert d.allowed is False
    assert "burn" in d.reason.lower() or "zero" in d.reason.lower()


def test_unpinned_rpc_still_refuses_for_a_native_send():
    d = tx_guard.authorize(
        _intent(), {"to": RELAY, "data": "0x", "value": AMOUNT, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: _deltas(),
        price_fn=lambda c, a: ETH_PRICE, fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: False, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)
    assert d.allowed is False
    assert "pinned rpc" in d.reason.lower()


def test_unpriceable_native_refuses_rather_than_valuing_at_zero():
    """An unpriced outflow booked at $0.00 would widen every cap by its own size."""
    d = _authorize(price=None)
    assert d.allowed is False
    assert "price" in d.reason.lower() or "value" in d.reason.lower()


def test_a_chain_with_no_wrapped_native_pin_refuses():
    """Solana carries no EVM wrapped-native the EVM rail can price, and an unknown
    chain carries no row at all. Either way the refusal must name the reason."""
    d = _authorize(intent=_intent(chain="nosuchchain"))
    assert d.allowed is False


def test_over_declared_max_spend_refuses():
    d = _authorize(intent=_intent(max_spend_usd=5.0))
    assert d.allowed is False
    assert "max_spend_usd" in d.reason


def test_above_the_autonomous_ceiling_routes_to_the_owner_queue():
    """$50 of native ETH is over the $25 default ceiling: not a refusal to report
    as failure, but the owner-queue lane."""
    big = 25 * 10 ** 15          # 0.025 ETH = $50
    d = _authorize(intent=_intent(amount_raw=big), deltas=_deltas(native_delta=-big))
    assert d.allowed is False
    assert d.lane == "owner_queue"
    assert d.amount_usd == pytest.approx(50.0)


# --------------------------------------------------------------------------
# The ERC-20 path is untouched
# --------------------------------------------------------------------------

def test_erc20_transfer_still_refuses_an_unexpected_native_move():
    """The dust refusal must survive for a token send — that is the case its own
    comment describes, and it is the one this change must not weaken."""
    d = tx_guard.authorize(
        tx_guard.TxIntent(chain="base", token=USDC, to=RELAY, amount_raw=250_000,
                          max_spend_usd=1.0, idempotency_key="erc20-1"),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: Deltas(ok=True, native_delta=-ONE_ETH,
                                       token_deltas={USDC: -250_000},
                                       allowance_deltas={}),
        price_fn=lambda c, a: 1.0, fallback_price_fn=None,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)
    assert d.allowed is False
    assert "native balance change" in d.reason.lower()
