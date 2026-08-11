"""tx_guard — the single choke point for value-moving transactions.

Every test here is a refusal. The guard's job is to say no; the happy path is
the exception, not the rule. Fail-closed is not configurable on this path.
"""
import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
SPENDER = "0x1111111111111111111111111111111111111111"
HOLDER = "0x2222222222222222222222222222222222222222"


def _intent(**kw):
    base = dict(chain="base", token=USDC, to=TO, amount_raw=250_000,
                max_spend_usd=0.25, expected_allowance_grants=(),
                idempotency_key="k1")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _clean_deltas(**kw):
    base = dict(ok=True, native_delta=0,
                token_deltas={USDC: -250_000},
                allowance_deltas={(USDC, SPENDER): 0})
    base.update(kw)
    return Deltas(**base)


def _gate():
    return PolicyGate(max_per_tx_usd=2.0, daily_cap_usd=10.0)


def _authorize(intent=None, deltas=None, *, gate=None, ctx=None,
               price=1.0, pinned_rpc=True, halted=False, forged=None):
    return tx_guard.authorize(
        intent or _intent(),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER,
        gate=gate or _gate(),
        execution_context=ctx,
        simulate_fn=lambda **_: deltas if deltas is not None else _clean_deltas(),
        price_fn=lambda chain, addr: price,
        rpc_is_pinned_fn=lambda chain: pinned_rpc,
        halted_fn=lambda: halted,
        forged_fn=(forged if forged is not None else (lambda ctx, tool: False)),
    )


# --------------------------------------------------------------------------
# Turn origin and kill switch
# --------------------------------------------------------------------------

def test_kill_switch_refuses():
    d = _authorize(halted=True)
    assert d.allowed is False
    assert "halt" in d.reason.lower()


def test_forged_turn_refuses():
    d = _authorize(ctx=object(), forged=lambda ctx, tool: True)
    assert d.allowed is False
    assert "forged" in d.reason.lower() or "autonomous" in d.reason.lower()


def test_unprovable_turn_origin_refuses():
    def boom(ctx, tool):
        raise RuntimeError("cannot resolve")
    d = _authorize(ctx=object(), forged=boom)
    assert d.allowed is False


# --------------------------------------------------------------------------
# RPC trust
# --------------------------------------------------------------------------

def test_default_public_rpc_refuses_to_arm():
    """Every enforcement point reads from the RPC, so a shared public endpoint
    cannot be the trust anchor for moving money."""
    d = _authorize(pinned_rpc=False)
    assert d.allowed is False
    assert "rpc" in d.reason.lower()


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

def test_failed_simulation_refuses():
    d = _authorize(deltas=Deltas(ok=False, error="execution reverted"))
    assert d.allowed is False
    assert "simulat" in d.reason.lower()


def test_outflow_larger_than_declared_refuses():
    d = _authorize(deltas=_clean_deltas(token_deltas={USDC: -900_000}))
    assert d.allowed is False
    assert "exceeds" in d.reason.lower() or "more than" in d.reason.lower()


def test_undeclared_allowance_grant_refuses():
    """The hidden-approve case — the one effect a USD cap cannot bound."""
    d = _authorize(deltas=_clean_deltas(
        allowance_deltas={(USDC, SPENDER): 5_000_000}))
    assert d.allowed is False
    assert "allowance" in d.reason.lower()


def test_declared_allowance_grant_within_bound_is_permitted():
    d = _authorize(
        intent=_intent(expected_allowance_grants=((USDC, SPENDER, 5_000_000),)),
        deltas=_clean_deltas(allowance_deltas={(USDC, SPENDER): 5_000_000}))
    assert d.allowed is True


def test_declared_allowance_grant_exceeded_refuses():
    d = _authorize(
        intent=_intent(expected_allowance_grants=((USDC, SPENDER, 1_000),)),
        deltas=_clean_deltas(allowance_deltas={(USDC, SPENDER): 5_000_000}))
    assert d.allowed is False


def test_unexpected_native_drain_refuses():
    d = _authorize(deltas=_clean_deltas(native_delta=-10 ** 18))
    assert d.allowed is False
    assert "native" in d.reason.lower()


# --------------------------------------------------------------------------
# Pricing and caps
# --------------------------------------------------------------------------

def test_unknown_price_refuses():
    """An unpriceable outflow cannot be checked against any cap."""
    d = _authorize(price=None)
    assert d.allowed is False
    assert "price" in d.reason.lower()


def test_outflow_over_declared_usd_refuses():
    d = _authorize(intent=_intent(max_spend_usd=0.10), price=1.0)
    assert d.allowed is False


def test_policygate_ceiling_refuses():
    gate = PolicyGate(max_per_tx_usd=0.05, daily_cap_usd=10.0)
    d = _authorize(gate=gate)
    assert d.allowed is False


def test_above_autonomous_cap_routes_to_owner_queue(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "0.10")
    d = _authorize()
    assert d.lane == "owner_queue"
    assert d.allowed is False, "an owner_queue lane is not an execute grant"


def test_within_autonomous_cap_is_allowed(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "25")
    d = _authorize()
    assert d.allowed is True
    assert d.lane == "autonomous"
    assert d.amount_usd == pytest.approx(0.25)


# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------

def test_zero_amount_refuses():
    d = _authorize(intent=_intent(amount_raw=0))
    assert d.allowed is False


def test_to_the_zero_address_refuses():
    d = _authorize(intent=_intent(to="0x" + "00" * 20))
    assert d.allowed is False
    assert "zero address" in d.reason.lower() or "burn" in d.reason.lower()


def test_missing_turn_origin_detector_refuses_when_a_context_is_present():
    """core cannot import the tools-tier detector, so the caller injects it.
    A context with no detector means we cannot prove the turn is genuine."""
    d = tx_guard.authorize(
        _intent(),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=object(),
        simulate_fn=lambda **_: _clean_deltas(),
        price_fn=lambda c, a: 1.0,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        forged_fn=None,
    )
    assert d.allowed is False
    assert "cannot prove" in d.reason.lower()


def test_no_context_is_a_direct_call_and_needs_no_detector():
    d = tx_guard.authorize(
        _intent(),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: _clean_deltas(),
        price_fn=lambda c, a: 1.0,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
    )
    assert d.allowed is True


def test_zero_measured_outflow_is_a_measurement_failure_not_a_free_transfer():
    """THE regression. On 2026-08-09 the delta engine used eth_call, which does
    not persist state, so every delta measured 0. The guard priced a real 0.25
    USDC transfer at $0.00 and authorized it through a cap that should have
    refused. Zero outflow on a declared send now refuses."""
    d = _authorize(deltas=_clean_deltas(token_deltas={USDC: 0}))
    assert d.allowed is False
    assert "measurement failure" in d.reason.lower() or "no outflow" in d.reason.lower()
