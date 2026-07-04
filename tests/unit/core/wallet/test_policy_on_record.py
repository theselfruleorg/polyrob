"""Wallet spend telemetry (telemetry audit 2026-07-04): PolicyGate must expose an
on_record hook so each value-moving action can be emitted to the durable event log
instead of living only in an in-memory audit list that vanishes on restart.
"""
from core.wallet.policy import PolicyGate


def test_on_record_fires_with_entry():
    seen = []
    gate = PolicyGate(max_per_tx_usd=100.0, on_record=lambda e: seen.append(e))
    gate.record(venue="x402", action="pay", amount_usd=5.0, counterparty="0xabc",
                idempotency_key="k1", result_ref="tx1")
    assert len(seen) == 1
    assert seen[0]["venue"] == "x402"
    assert seen[0]["amount_usd"] == 5.0
    assert seen[0]["action"] == "pay"


def test_on_record_failure_is_fail_open():
    def boom(_):
        raise RuntimeError("sink down")
    gate = PolicyGate(max_per_tx_usd=100.0, on_record=boom)
    # A crashing telemetry sink must NOT break the wallet record path.
    gate.record(venue="x402", action="pay", amount_usd=1.0, counterparty=None,
                idempotency_key=None, result_ref=None)
    assert gate.audit_log[-1]["amount_usd"] == 1.0


def test_no_on_record_still_works():
    gate = PolicyGate(max_per_tx_usd=100.0)
    gate.record(venue="hyperliquid", action="trade", amount_usd=2.0, counterparty=None,
                idempotency_key=None, result_ref=None)
    assert gate.audit_log[-1]["venue"] == "hyperliquid"
