"""Invalid money values must not turn comparisons or rolling caps into bypasses."""
import pytest

from core.wallet.policy import PolicyGate


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -float("inf"), -1, True, None, "bad"])
def test_invalid_spend_is_refused(amount):
    gate = PolicyGate(100, daily_cap_usd=100)
    assert not gate.check(venue="x402", amount_usd=amount, idempotency_key=None).allowed


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, True])
def test_invalid_record_cannot_poison_ledger(amount):
    gate = PolicyGate(100, daily_cap_usd=100)
    with pytest.raises(ValueError):
        gate.record(venue="x402", action="pay", amount_usd=amount,
                    counterparty=None, idempotency_key="key", result_ref=None)
    assert not gate.audit_log
    assert gate.check(venue="x402", amount_usd=1, idempotency_key="key").allowed


@pytest.mark.parametrize("field", ["max_per_tx_usd", "daily_cap_usd", "per_venue_daily_cap_usd"])
@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1])
def test_invalid_cap_configuration_fails_closed(field, amount):
    kwargs = {"max_per_tx_usd": 100}
    kwargs[field] = {"x402": amount} if field == "per_venue_daily_cap_usd" else amount
    with pytest.raises(ValueError):
        PolicyGate(**kwargs)


def test_venue_case_does_not_reset_daily_cap():
    gate = PolicyGate(100, per_venue_daily_cap_usd={"x402": 10})
    gate.record(venue="X402", action="pay", amount_usd=8,
                counterparty=None, idempotency_key=None, result_ref=None)
    assert not gate.check(venue="x402", amount_usd=8, idempotency_key=None).allowed
    assert not gate.check(venue="X402", amount_usd=8, idempotency_key=None).allowed
