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
               price=1.0, fallback_price="_unset", pinned_rpc=True, halted=False,
               entry_paused=False, forged=None):
    return tx_guard.authorize(
        intent or _intent(),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER,
        gate=gate or _gate(),
        execution_context=ctx,
        simulate_fn=lambda **_: deltas if deltas is not None else _clean_deltas(),
        price_fn=lambda chain, addr: price,
        fallback_price_fn=(
            None if fallback_price == "_unset" else (lambda chain, addr: fallback_price)),
        rpc_is_pinned_fn=lambda chain: pinned_rpc,
        halted_fn=lambda: halted,
        entry_paused_fn=lambda: entry_paused,
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
# Entry-pause (2026-08-28): refuses NEW positions, exits still pass
# --------------------------------------------------------------------------

def test_entry_pause_refuses_a_non_exit_intent():
    """A plain transfer/entry-shaped intent (the default `_intent()`, not a
    sell of a held token into the quote asset) is refused while paused."""
    d = _authorize(entry_paused=True)
    assert d.allowed is False
    assert "pause" in d.reason.lower()


def test_entry_pause_allows_an_exit_bounded_approve():
    """An allowance grant that cannot exceed the wallet's own held balance is
    exit-bounded (028) and still runs while entries are paused."""
    exit_intent = _approve_intent(grant=1_000_000, held_balance_raw=1_000_000)
    d = _authorize(intent=exit_intent, entry_paused=True,
                   deltas=_approve_deltas(grant=1_000_000), price=1.0)
    assert d.allowed is True, d.reason


def test_entry_pause_allows_a_revoke():
    """A revoke (allowance op, no grants) is always exit-shaped."""
    revoke_intent = tx_guard.TxIntent(
        chain="base", token=USDC, to=SPENDER, amount_raw=0,
        max_spend_usd=2.0, is_allowance_op=True,
        expected_allowance_grants=(), idempotency_key="krevoke")
    d = _authorize(intent=revoke_intent, entry_paused=True,
                   deltas=Deltas(ok=True, native_delta=0, token_deltas={USDC: 0},
                                allowance_deltas={(USDC, SPENDER): 0}))
    assert d.allowed is True, d.reason


def test_entry_pause_off_by_default():
    d = _authorize()
    assert d.allowed is True, d.reason


def test_entry_pause_probe_failure_refuses_closed():
    def boom():
        raise RuntimeError("probe unavailable")
    d = tx_guard.authorize(
        _intent(),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=lambda **_: _clean_deltas(),
        price_fn=lambda chain, addr: 1.0,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        entry_paused_fn=boom,
        forged_fn=lambda ctx, tool: False,
    )
    assert d.allowed is False
    assert "entry-pause" in d.reason.lower() or "failing closed" in d.reason.lower()


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


# 028 (2026-08-22): the same exit exemption applies to a bare OUTFLOW (a
# swap's token_in leg), not just an allowance grant — a swap TxIntent is not
# is_allowance_op and hits this branch, which the first cut of 028 missed
# (found live: BPAD's approve went through, the swap itself still refused).

def test_outflow_within_held_balance_uses_fallback_price_when_unpriceable():
    d = _authorize(intent=_intent(held_balance_raw=250_000, max_spend_usd=1.0),
                   price=None, fallback_price=2.0)
    assert d.allowed is True, d.reason
    assert d.amount_usd == pytest.approx(0.5)   # 0.25 token at the $2 fallback


def test_outflow_exceeding_held_balance_still_refuses():
    d = _authorize(intent=_intent(held_balance_raw=249_999), price=None,
                   fallback_price=2.0)
    assert d.allowed is False
    assert "price" in d.reason.lower()


def test_outflow_exemption_is_inert_without_held_balance_declared():
    d = _authorize(intent=_intent(), price=None, fallback_price=2.0)
    assert d.allowed is False
    assert "price" in d.reason.lower()


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


def test_decision_carries_the_simulations_gas_used():
    """The rail sizes the broadcast gas limit from the simulation's gasUsed
    (§3a — a fixed limit out-of-gas-reverts a swap and burns the fee), so an
    allowed Decision must carry the measurement out of the guard."""
    d = _authorize(deltas=_clean_deltas(gas_used=137_000))
    assert d.allowed is True
    assert d.sim_gas_used == 137_000


def test_decision_gas_defaults_to_none_when_unmeasured():
    d = _authorize()
    assert d.allowed is True
    assert d.sim_gas_used is None


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


# --------------------------------------------------------------------------
# Allowance ops (023 T4) — the REAL approve/revoke shape, end to end.
#
# An approve is: amount_raw=0, token delta 0, allowance delta +grant. The
# is_allowance_op fix (fda4c9f7) repaired the structural amount>0 rule but the
# delta-assert step still read the zero token delta as a measurement failure,
# so with a pinned RPC EVERY approve/revoke was dead on arrival. These tests
# run the full authorize() with the exact deltas a real approval produces.
# --------------------------------------------------------------------------

def _approve_intent(grant=1_000_000, **kw):
    base = dict(chain="base", token=USDC, to=SPENDER, amount_raw=0,
                max_spend_usd=2.0, is_allowance_op=True,
                expected_allowance_grants=((USDC, SPENDER, grant),),
                idempotency_key="ka1")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _approve_deltas(grant=1_000_000, moved=0):
    return Deltas(ok=True, native_delta=0, token_deltas={USDC: moved},
                  allowance_deltas={(USDC, SPENDER): grant})


def test_a_real_approve_shape_is_authorized():
    """The single most important allowed-path test in this file: the exact
    deltas a genuine ERC-20 approve produces must clear the guard."""
    d = _authorize(intent=_approve_intent(), deltas=_approve_deltas(), price=1.0)
    assert d.allowed is True, d.reason
    assert d.lane == "autonomous"


def test_an_approve_prices_the_grant_not_the_zero_outflow():
    """§1.1 root fix: the risk of an approval is the DECLARED GRANT. Pricing
    the (zero) outflow valued every approval at $0.00 and no cap could bound it."""
    d = _authorize(intent=_approve_intent(grant=1_000_000),
                   deltas=_approve_deltas(grant=1_000_000), price=1.0)
    assert d.amount_usd == pytest.approx(1.0)   # 1.0 USDC at $1, not $0.00


def test_an_unpriceable_approval_grant_is_refused():
    """§1.1: the memecoin case. A low-confidence price is None, and an
    unpriceable grant must REFUSE — not silently skip the USD bound."""
    d = _authorize(intent=_approve_intent(), deltas=_approve_deltas(), price=None)
    assert d.allowed is False
    assert "price" in d.reason.lower()


# --------------------------------------------------------------------------
# 028 (2026-08-22): exit-bounded allowance exemption. A grant that cannot
# exceed the wallet's OWN held balance of the token puts no NEW value at
# risk, so it may be valued via a best-effort fallback price instead of
# requiring the high-confidence bar — narrow, and additive (default None =
# byte-identical to pre-028 behaviour, see test above).
# --------------------------------------------------------------------------

def test_exit_within_held_balance_uses_fallback_price_when_unpriceable():
    d = _authorize(
        intent=_approve_intent(grant=1_000_000, held_balance_raw=1_000_000),
        deltas=_approve_deltas(grant=1_000_000), price=None, fallback_price=2.0)
    assert d.allowed is True, d.reason
    assert d.amount_usd == pytest.approx(2.0)   # 1.0 token at the $2 fallback price


def test_exit_grant_exceeding_held_balance_still_refuses():
    """held_balance_raw bounds the exemption tightly — one wei over refuses
    exactly like the pre-028 path, not a rounding-tolerant near-miss."""
    d = _authorize(
        intent=_approve_intent(grant=1_000_000, held_balance_raw=999_999),
        deltas=_approve_deltas(grant=1_000_000), price=None, fallback_price=2.0)
    assert d.allowed is False
    assert "price" in d.reason.lower()


def test_exit_exemption_without_a_fallback_price_values_at_zero():
    """2026-08-26 exit untying: an exit-bounded grant NO source can price is
    allowed at $0 instead of refusing — the old refusal left fired stop rules
    (BPAD, BaseUnc at −54%) with no autonomous exit path at all. The swap that
    follows is valued exactly (measured quote inflow) and capped; the grant is
    still simulation-asserted to the declared spender and amount."""
    d = _authorize(
        intent=_approve_intent(grant=1_000_000, held_balance_raw=1_000_000),
        deltas=_approve_deltas(grant=1_000_000), price=None, fallback_price=None)
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0


def test_exit_exemption_is_inert_without_held_balance_declared():
    """The default (held_balance_raw=None) is unaffected even when a
    fallback price IS available — the caller must opt in explicitly."""
    d = _authorize(intent=_approve_intent(), deltas=_approve_deltas(),
                   price=None, fallback_price=2.0)
    assert d.allowed is False
    assert "price" in d.reason.lower()


def test_exit_exemption_still_enforces_the_usd_and_policygate_caps():
    """The exemption only waives the CONFIDENCE bar — every downstream cap
    (max_spend_usd, PolicyGate, autonomous ceiling) still runs off whatever
    the fallback price computes."""
    d = _authorize(
        intent=_approve_intent(grant=5_000_000, held_balance_raw=5_000_000,
                               max_spend_usd=1.0),
        deltas=_approve_deltas(grant=5_000_000), price=None, fallback_price=2.0)
    assert d.allowed is False
    assert "max_spend_usd" in d.reason or "exceeds" in d.reason.lower()


def test_an_approval_grant_above_the_declared_usd_refuses():
    d = _authorize(intent=_approve_intent(grant=5_000_000, max_spend_usd=1.0),
                   deltas=_approve_deltas(grant=5_000_000), price=1.0)
    assert d.allowed is False
    assert "max_spend_usd" in d.reason or "exceeds" in d.reason.lower()


def test_an_approval_grant_hits_the_policygate_ceiling():
    gate = PolicyGate(max_per_tx_usd=0.5, daily_cap_usd=10.0)
    d = _authorize(intent=_approve_intent(grant=1_000_000),
                   deltas=_approve_deltas(grant=1_000_000), price=1.0, gate=gate)
    assert d.allowed is False


def test_an_approval_grant_above_the_autonomous_ceiling_routes_owner_queue(monkeypatch):
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "0.10")
    d = _authorize(intent=_approve_intent(grant=1_000_000),
                   deltas=_approve_deltas(grant=1_000_000), price=1.0)
    assert d.allowed is False
    assert d.lane == "owner_queue"


def test_an_allowance_op_that_moves_tokens_is_refused():
    """An approve/revoke must move NOTHING. A token delta on an allowance op is
    undeclared behaviour, whichever direction it points."""
    d = _authorize(intent=_approve_intent(), deltas=_approve_deltas(moved=-100),
                   price=1.0)
    assert d.allowed is False
    d = _authorize(intent=_approve_intent(), deltas=_approve_deltas(moved=100),
                   price=1.0)
    assert d.allowed is False


def test_a_real_revoke_shape_is_authorized_without_any_price():
    """A revoke declares no grant, so its risk is $0 and it must never be
    refused for pricing — revoking a worthless/unpriceable token is exactly
    the cleanup the allowance-hygiene design wants to stay easy."""
    intent = _approve_intent(expected_allowance_grants=(), max_spend_usd=0.01)
    deltas = Deltas(ok=True, native_delta=0, token_deltas={USDC: 0},
                    allowance_deltas={(USDC, SPENDER): -1_000_000})
    d = _authorize(intent=intent, deltas=deltas, price=None)
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0


# --------------------------------------------------------------------------
# Event-log cross-check (2026-08-14 T4 review) — the reads only cover the
# DECLARED token and measured spenders; the tx's own event log is what covers
# a grant to an undeclared spender or a drain of an undeclared token.
# --------------------------------------------------------------------------

OTHER_SPENDER = "0x3333333333333333333333333333333333333333"
OTHER_TOKEN = "0x4444444444444444444444444444444444444444"


def test_an_approval_event_to_an_unmeasured_spender_refuses():
    """The reads never measured this pair, so the read-based check is blind to
    it — the event is the only witness, and it refuses."""
    d = _authorize(deltas=_clean_deltas(
        holder_approvals=((USDC.lower(), OTHER_SPENDER, 5_000_000),)))
    assert d.allowed is False
    assert "approval" in d.reason.lower()


def test_an_approval_event_on_a_measured_pair_defers_to_the_read_delta():
    """Some tokens re-emit Approval(remaining) on transferFrom — a DECREASE.
    On a measured pair the read delta is authoritative, so the event alone
    must not refuse."""
    d = _authorize(deltas=_clean_deltas(
        holder_approvals=((USDC.lower(), SPENDER.lower(), 750_000),)))
    assert d.allowed is True, d.reason


def test_a_transfer_event_on_an_undeclared_token_refuses():
    """A tx that also drains a token the intent never mentioned was invisible
    to the balance reads (only intent.token is measured)."""
    d = _authorize(deltas=_clean_deltas(
        holder_transfers=((OTHER_TOKEN, TO.lower(), 999),)))
    assert d.allowed is False
    assert "undeclared token" in d.reason.lower()


def test_a_transfer_event_on_the_declared_token_is_fine():
    d = _authorize(deltas=_clean_deltas(
        holder_transfers=((USDC.lower(), TO.lower(), 250_000),)))
    assert d.allowed is True, d.reason


def test_watch_spenders_are_measured_without_declaring_a_grant():
    """The swap intent watches the router pair so its read delta (a decrease)
    is judged as a decrease — and so a genuine hidden INCREASE on that pair is
    caught by the read-based check."""
    seen = {}

    def sim(**kw):
        seen["spenders"] = kw.get("spenders")
        return _clean_deltas()

    d = tx_guard.authorize(
        _intent(watch_spenders=(OTHER_SPENDER,)),
        {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=_gate(), execution_context=None,
        simulate_fn=sim, price_fn=lambda c, a: 1.0,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False)
    assert seen["spenders"] == [OTHER_SPENDER]
    assert d.allowed is True


# --------------------------------------------------------------------------
# 029 R6: an unpriceable refusal must say WHICH lever is missing
#
# The agent wrote a complete design proposal (reports/2026-08-22-tx-guard-fix-
# proposal.md) for a deadlock that had already shipped as 028 and was live on
# the deployed SHA. It could not tell, because the refusal string never changed:
# "no trustworthy price" reads identically whether the exit exemption was never
# eligible, was eligible but unfunded by a fallback price, or does not exist at
# all. Three different situations, three different next moves, one sentence.
# --------------------------------------------------------------------------

def test_an_unpriceable_grant_with_no_held_balance_says_the_exemption_needs_one():
    """held_balance_raw is None: the exit exemption was never eligible. The
    agent's next move is to read its balance, not to file a proposal."""
    d = _authorize(intent=_approve_intent(held_balance_raw=None),
                   deltas=_approve_deltas(), price=None)
    assert not d.allowed
    assert "held balance" in d.reason.lower()


def test_an_unpriceable_exit_bounded_grant_is_allowed_at_zero():
    """2026-08-26 exit untying: eligible for the exemption and priceless by
    every source — that used to dead-end on 'have the owner approve it by
    hand', which on prod meant fired stop rules could never execute. Now $0,
    loudly, and only for grants within the held balance."""
    d = _authorize(intent=_approve_intent(held_balance_raw=10_000_000),
                   deltas=_approve_deltas(), price=None, fallback_price=None)
    assert d.allowed is True, d.reason
    assert d.amount_usd == 0.0


def test_the_unpriceable_refusal_still_refuses_without_a_held_balance():
    """The untying is bounded by ownership: with no declared held balance the
    grant could put MORE at risk than is owned, and it still refuses."""
    d = _authorize(intent=_approve_intent(held_balance_raw=None),
                   deltas=_approve_deltas(), price=None, fallback_price=None)
    assert not d.allowed
    assert "held balance" in d.reason.lower()


def test_an_exit_bounded_grant_that_the_fallback_CAN_price_still_passes():
    """028's exemption must survive the message change untouched."""
    d = _authorize(intent=_approve_intent(grant=1_000_000, held_balance_raw=2_000_000),
                   deltas=_approve_deltas(grant=1_000_000), price=None,
                   fallback_price=0.000001)
    assert d.allowed is True, d.reason
