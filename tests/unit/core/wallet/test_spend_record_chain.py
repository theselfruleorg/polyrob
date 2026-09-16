"""The spend record carries the chain and the right address (043 A36).

The one durable money record is the `wallet_spend` telemetry event written by
`PolicyGate.record` through the `on_record` hook in `core/wallet/factory.py`.
It carried `venue, action, amount_usd, counterparty, result_ref, ts` and no
`chain` — so a status/creations view could not say WHERE something landed.
"""
from core.wallet.policy import PolicyGate


def test_record_carries_chain():
    seen = []
    gate = PolicyGate(max_per_tx_usd=250, daily_cap_usd=100, on_record=seen.append)
    gate.record(venue="defi", action="swap", amount_usd=1.0, counterparty="0xabc",
                idempotency_key=None, result_ref="0xtx", chain="robinhood")
    assert seen[0]["chain"] == "robinhood"


def test_record_without_chain_defaults_to_none():
    """Back-compat: an existing call site that never passes `chain=` must not
    break — the field is optional and defaults to None."""
    seen = []
    gate = PolicyGate(max_per_tx_usd=250, on_record=seen.append)
    gate.record(venue="x402", action="pay", amount_usd=1.0, counterparty=None,
                idempotency_key=None, result_ref=None)
    assert seen[0]["chain"] is None
