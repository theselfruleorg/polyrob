"""Untying exits (2026-08-26, owner directive: "turn off superficial guards").

A week of prod logs showed the guard refusing CLOSES, not attacks: BPAD and
BaseUnc sat with fired stop rules (−54%) that could not execute because the
sell-side needed SOME price for the cap arithmetic and every price source had
none; a stop noticed on a self-wake monitor tick was refused by the turn-origin
bar; a confirmed approve consumed the daily cap the swap then needed.

The untying is NARROW and the load-bearing bars are untouched: simulation,
delta assertion, undeclared-allowance refusal, the kill switch, caps, and the
forged-turn refusal for anything that is not an exit.

New rules pinned here:
1. A sell of a held token to the chain's QUOTE ASSET is valued at the
   simulation's MEASURED inflow when the outflow token has no price by any
   source — the treasury's receipt is exact and unfalsifiable, so the caps
   run against it instead of refusing.
2. An exit-bounded allowance grant on an unpriceable token values at $0
   (loudly) instead of dead-ending on "have the owner approve it by hand".
3. Cap comparisons are run in CENTS — a $1.9903-vs-$1.99 refusal is a rounding
   artifact, not a policy.
4. DEFI_MONITOR_EXITS (default OFF) lets a forged MAIN-agent turn (self-wake /
   delegation-result — the monitor loop) execute EXIT-shaped operations only:
   sell-to-quote within held balance, exit-bounded approve, revoke. Entries
   stay refused on those turns.
"""
import types

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # base's pinned quote asset
MEME = "0xB2000000000000000000000Ff4a547c891AB1b01"
SPENDER = "0x1111111111111111111111111111111111111111"
HOLDER = "0x2222222222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def _decimals(monkeypatch):
    monkeypatch.setattr(tx_guard, "_decimals_for",
                        lambda chain, token: 6 if token.lower() == USDC.lower() else 18)


def _gate(per_tx=5.0, daily=25.0):
    return PolicyGate(max_per_tx_usd=per_tx, daily_cap_usd=daily)


def _sell_intent(**kw):
    base = dict(chain="base", token=MEME, to=SPENDER, amount_raw=10 ** 18,
                max_spend_usd=2.0, watch_spenders=(SPENDER,),
                idempotency_key="k1", held_balance_raw=10 ** 18,
                inflow_token=USDC)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _sell_deltas(usdc_in=2_000_000, meme_out=-(10 ** 18), **kw):
    base = dict(ok=True, native_delta=0,
                token_deltas={MEME: meme_out, USDC: usdc_in},
                allowance_deltas={(MEME, SPENDER): 0})
    base.update(kw)
    return Deltas(**base)


def _authorize(intent, deltas, *, gate=None, ctx=None, forged=False,
               autonomous_ok=False, price=None, fallback=None):
    return tx_guard.authorize(
        intent, {"to": SPENDER, "data": "0x", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=gate or _gate(),
        execution_context=ctx,
        simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: price,
        fallback_price_fn=lambda chain, addr: fallback,
        rpc_is_pinned_fn=lambda chain: True,
        halted_fn=lambda: False,
        forged_fn=lambda _ec, _ts: forged,
        autonomous_ok_fn=lambda _ec, _ts: autonomous_ok,
    )


# --------------------------------------------------------------------------
# 1. Exit valuation by measured quote-asset inflow
# --------------------------------------------------------------------------

def test_unpriceable_sell_to_quote_is_valued_at_measured_inflow():
    """The BPAD/BaseUnc deadlock: stop fired, no price by ANY source, exit
    refused. The measured USDC receipt is the honest cap number."""
    d = _authorize(_sell_intent(), _sell_deltas(usdc_in=2_000_000))
    assert d.allowed, d.reason
    assert d.amount_usd == pytest.approx(2.00)


def test_unpriceable_sell_without_declared_inflow_still_refuses():
    d = _authorize(_sell_intent(inflow_token=None), _sell_deltas())
    assert not d.allowed
    assert "no trustworthy price" in d.reason


def test_unpriceable_sell_with_zero_measured_inflow_refuses():
    """No measured receipt = a measurement failure, never a free pass."""
    d = _authorize(_sell_intent(), _sell_deltas(usdc_in=0))
    assert not d.allowed


def test_inflow_valuation_still_hits_the_ceiling():
    """The caps run AGAINST the measured receipt — untied, not uncapped."""
    d = _authorize(_sell_intent(max_spend_usd=50.0),
                   _sell_deltas(usdc_in=12_000_000), gate=_gate(per_tx=5.0))
    assert not d.allowed
    assert "PolicyGate" in d.reason


def test_a_sell_beyond_held_balance_is_not_an_exit():
    d = _authorize(_sell_intent(held_balance_raw=10 ** 17), _sell_deltas())
    assert not d.allowed


# --------------------------------------------------------------------------
# 2. Exit-bounded unpriceable allowance grant values at $0
# --------------------------------------------------------------------------

def _approve_intent(**kw):
    base = dict(chain="base", token=MEME, to=SPENDER, amount_raw=0,
                max_spend_usd=2.0,
                expected_allowance_grants=((MEME, SPENDER, 10 ** 18),),
                is_allowance_op=True, idempotency_key="k2",
                held_balance_raw=10 ** 18)
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _approve_deltas():
    return Deltas(ok=True, native_delta=0, token_deltas={MEME: 0},
                  allowance_deltas={(MEME, SPENDER): 10 ** 18})


def test_unpriceable_exit_bounded_grant_is_allowed_at_zero():
    d = _authorize(_approve_intent(), _approve_deltas())
    assert d.allowed, d.reason
    assert d.amount_usd == 0.0


def test_unpriceable_grant_beyond_held_balance_still_refuses():
    d = _authorize(_approve_intent(held_balance_raw=10 ** 17), _approve_deltas())
    assert not d.allowed
    assert "no trustworthy price" in d.reason


# --------------------------------------------------------------------------
# 3. Cents, not sub-cent noise
# --------------------------------------------------------------------------

def test_sub_cent_drift_does_not_refuse_the_declared_max():
    """$1.9903 vs a declared $1.99 was a live refusal → retry dance. Caps
    operate in cents."""
    intent = _sell_intent(amount_raw=1_990_300, held_balance_raw=2_000_000,
                          token=USDC, inflow_token=None, max_spend_usd=1.99)
    deltas = Deltas(ok=True, native_delta=0, token_deltas={USDC: -1_990_300},
                    allowance_deltas={(USDC, SPENDER): 0})
    d = _authorize(intent, deltas, price=1.0)
    assert d.allowed, d.reason


# --------------------------------------------------------------------------
# 4. DEFI_MONITOR_EXITS — the monitor loop may CLOSE, never OPEN
# --------------------------------------------------------------------------

def _monitor_ctx():
    return types.SimpleNamespace(role="orchestrator", is_sub_agent=False)


class TestMonitorExitsOff:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch):
        monkeypatch.delenv("DEFI_MONITOR_EXITS", raising=False)

    def test_a_self_wake_exit_is_still_refused(self):
        d = _authorize(_sell_intent(), _sell_deltas(), ctx=_monitor_ctx(),
                       forged=True, autonomous_ok=False)
        assert not d.allowed
        assert "forged" in d.reason.lower()


class TestMonitorExitsOn:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")

    def test_a_self_wake_sell_to_quote_within_held_balance_passes(self):
        d = _authorize(_sell_intent(), _sell_deltas(), ctx=_monitor_ctx(),
                       forged=True, autonomous_ok=False)
        assert d.allowed, d.reason

    def test_a_self_wake_ENTRY_is_still_refused(self):
        """Buying MEME with USDC is not an exit — inflow is not the quote."""
        intent = _sell_intent(token=USDC, inflow_token=MEME,
                              amount_raw=1_000_000, held_balance_raw=2_000_000)
        deltas = Deltas(ok=True, native_delta=0,
                        token_deltas={USDC: -1_000_000, MEME: 10 ** 18},
                        allowance_deltas={(USDC, SPENDER): 0})
        d = _authorize(intent, deltas, ctx=_monitor_ctx(), forged=True,
                       price=1.0)
        assert not d.allowed
        assert "forged" in d.reason.lower()

    def test_a_self_wake_exit_bounded_approve_passes(self):
        d = _authorize(_approve_intent(), _approve_deltas(), ctx=_monitor_ctx(),
                       forged=True)
        assert d.allowed, d.reason

    def test_a_self_wake_revoke_passes(self):
        intent = _approve_intent(expected_allowance_grants=())
        deltas = Deltas(ok=True, native_delta=0, token_deltas={MEME: 0},
                        allowance_deltas={(MEME, SPENDER): 0})
        d = _authorize(intent, deltas, ctx=_monitor_ctx(), forged=True)
        assert d.allowed, d.reason

    def test_a_self_wake_over_grant_approve_is_refused(self):
        d = _authorize(_approve_intent(held_balance_raw=10 ** 17),
                       _approve_deltas(), ctx=_monitor_ctx(), forged=True)
        assert not d.allowed

    def test_a_sub_agent_exit_is_still_refused(self):
        ctx = types.SimpleNamespace(role="orchestrator", is_sub_agent=True)
        d = _authorize(_sell_intent(), _sell_deltas(), ctx=ctx, forged=True)
        assert not d.allowed

    def test_the_monitor_lane_demands_a_daily_cap(self):
        """The lane rides the autonomous origin: no aggregate damage bound, no
        unattended exit."""
        d = _authorize(_sell_intent(), _sell_deltas(), ctx=_monitor_ctx(),
                       forged=True, gate=PolicyGate(max_per_tx_usd=5.0))
        assert not d.allowed
        assert "daily" in d.reason.lower() or "aggregate" in d.reason.lower()
