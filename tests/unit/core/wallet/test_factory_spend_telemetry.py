"""Wallet spend → durable event log wiring (telemetry audit 2026-07-04).

The factory's PolicyGate now carries an on_record hook that mirrors each spend into
the TelemetryEventLog, so money movement is durable + queryable cross-session.
"""


def test_factory_gate_emits_wallet_spend(tmp_path, monkeypatch):
    import agents.task.telemetry.event_log as el

    monkeypatch.setattr(el, "_INSTANCES", {})
    test_log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: test_log)

    from core.wallet import factory
    factory.reset_agent_wallet_cache()
    try:
        gate = factory.get_policy_gate()
        gate.record(venue="x402", action="pay", amount_usd=7.0, counterparty=None,
                    idempotency_key=None, result_ref="tx9")
        rows = test_log.query(kind="wallet_spend")
        assert rows, "wallet_spend not recorded in event log"
        assert rows[0]["attrs"]["amount_usd"] == 7.0
        assert rows[0]["attrs"]["venue"] == "x402"
        assert rows[0]["source"] == "wallet"
    finally:
        factory.reset_agent_wallet_cache()
