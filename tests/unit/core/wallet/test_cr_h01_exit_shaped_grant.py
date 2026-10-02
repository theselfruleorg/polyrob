"""CR-H01: an approve of a held token to an ARBITRARY spender is not exit-shaped.

``_intent_is_exit_shaped`` used to accept any grant on the held token within the
held balance, so an injected "approve the whole position to 0xattacker" passed
the owner entry-pause and the DEFI_MONITOR_EXITS forged-turn lane. It now
demands exactly what ``_grant_is_exit_bounded`` demands.
"""
import types

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

MEME = "0xB2000000000000000000000Ff4a547c891AB1b01"
HOLDER = "0x2222222222222222222222222222222222222222"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"  # base pinned SwapRouter02
ATTACKER = "0x9999999999999999999999999999999999999999"


@pytest.fixture(autouse=True)
def _decimals(monkeypatch):
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda chain, token: 18)


def _intent(spender, *, to=None):
    return tx_guard.TxIntent(
        chain="base", token=MEME, to=to or spender, amount_raw=0,
        max_spend_usd=2.0, expected_allowance_grants=((MEME, spender, 10 ** 18),),
        is_allowance_op=True, idempotency_key="k", held_balance_raw=10 ** 18)


def _deltas(spender):
    return Deltas(ok=True, native_delta=0, token_deltas={MEME: 0},
                  allowance_deltas={(MEME, spender): 10 ** 18})


def _authorize(intent, deltas, *, ctx=None, forged=False, entry_paused=False):
    return tx_guard.authorize(
        intent, {"to": intent.to, "data": "0x", "value": 0, "chainId": 8453},
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=5.0, daily_cap_usd=25.0),
        execution_context=ctx, simulate_fn=lambda **_: deltas,
        price_fn=lambda c, a: 0.000001, fallback_price_fn=lambda c, a: None,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: entry_paused,
        forged_fn=lambda _e, _t: forged, autonomous_ok_fn=lambda _e, _t: False)


def test_predicate_rejects_arbitrary_spender():
    assert not tx_guard._intent_is_exit_shaped(_intent(ATTACKER))


def test_predicate_rejects_spender_not_the_declared_destination():
    assert not tx_guard._intent_is_exit_shaped(_intent(ROUTER, to=ATTACKER))


def test_predicate_accepts_pinned_router_within_held():
    assert tx_guard._intent_is_exit_shaped(_intent(ROUTER))


def test_entry_pause_refuses_grant_to_arbitrary_spender():
    d = _authorize(_intent(ATTACKER), _deltas(ATTACKER), entry_paused=True)
    assert not d.allowed
    assert "entry-pause" in d.reason


def test_entry_pause_still_passes_router_exit_grant():
    d = _authorize(_intent(ROUTER), _deltas(ROUTER), entry_paused=True)
    assert d.allowed, d.reason


def test_monitor_exit_lane_refuses_grant_to_arbitrary_spender(monkeypatch):
    monkeypatch.setenv("DEFI_MONITOR_EXITS", "true")
    ctx = types.SimpleNamespace(user_id="local", role="orchestrator", is_sub_agent=False)
    d = _authorize(_intent(ATTACKER), _deltas(ATTACKER), ctx=ctx, forged=True)
    assert not d.allowed
    assert "forged" in d.reason.lower()
