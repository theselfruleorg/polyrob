"""A wallet sends any token it holds (owner, 2026-10-17).

Prod: six USDG bounty payouts on Robinhood refused for "no trustworthy price"
(one pool, $1.26M deep) and the owner had no way to send them. A plain send
with no spend-grade price now needs the owner's extra confirmation
(``value_ack``) and runs on an owner lane only; it is never valued as free.
"""
from types import SimpleNamespace

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas
from core.wallet.tx_guard_steps.value import UNVERIFIED_MARK

TOKEN = "0x4444444444444444444444444444444444444444"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
HOLDER = "0x2222222222222222222222222222222222222222"
OWNER = "owner-1"
WEI = 10 ** 18


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    import importlib
    money_regime = importlib.import_module("core.config_policy.money_regime")
    monkeypatch.setattr(money_regime, "unattended_money_allowed", lambda: True)
    for k in ("DEFI_AUTONOMOUS_TURN_TRADING", "DEFI_MONITOR_EXITS", "WALLET_SIGNER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "5")
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: OWNER)
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: tmp_path / "prefs")
    real = tx_guard._decimals_for
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda chain, token: (
        18 if str(token).lower() == TOKEN.lower() else real(chain, token)))
    monkeypatch.setattr("core.security.refusals.record_refusal", lambda *a, **k: None)


def _owner():
    return SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                           session_id="s-owner", metadata={})


def _run(*, ack, fallback=None, ctx="owner", is_transfer=True, amount=25 * WEI):
    intent = tx_guard.TxIntent(
        chain="base", token=TOKEN, to=TO, amount_raw=amount, max_spend_usd=100.0,
        idempotency_key=f"k-{ack}-{fallback}-{ctx}", is_transfer=is_transfer,
        value_ack=ack)
    tx = {"to": TOKEN, "data": "0xa9059cbb", "value": 0, "chainId": 8453}
    deltas = Deltas(ok=True, native_delta=0, token_deltas={TOKEN: -amount})
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=PolicyGate(max_per_tx_usd=1000.0, daily_cap_usd=5000.0),
        execution_context=_owner() if ctx == "owner" else ctx,
        simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: None,          # no spend-grade price
        fallback_price_fn=(None if fallback is None else (lambda chain, addr: fallback)),
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda c, t: False)


def test_without_the_ack_it_refuses_and_names_the_extra_step():
    d = _run(ack=False, fallback=1.0)
    assert d.allowed is False
    assert UNVERIFIED_MARK in d.reason and "anyway" in d.reason


def test_owner_ack_with_a_weak_price_sends_at_that_estimate():
    d = _run(ack=True, fallback=1.0)
    assert d.allowed is True, d.reason
    assert d.lane == "owner_direct"
    assert d.valuation_basis == "low_confidence"
    assert d.amount_usd == pytest.approx(25.0, abs=0.01)


def test_owner_ack_with_no_price_at_all_is_never_free():
    d = _run(ack=True, fallback=None)
    assert d.allowed is True, d.reason
    assert d.valuation_basis == "unpriced"
    assert d.amount_usd >= 5.01                     # the ceiling plus a cent


def test_not_the_owner_goes_to_the_owner_never_autonomous():
    d = _run(ack=True, fallback=1.0, ctx=None)      # the remote signer / no context
    assert d.allowed is False
    assert d.lane == "owner_queue"
    assert UNVERIFIED_MARK in d.reason


def test_only_a_plain_send_gets_the_rule():
    d = _run(ack=True, fallback=1.0, is_transfer=False)
    assert d.allowed is False
    assert UNVERIFIED_MARK not in d.reason          # the old refusal, unchanged
