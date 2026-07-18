import core.wallet.factory as f


def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    f.reset_agent_wallet_cache()
    assert f.get_agent_wallet() is None


def test_enabled_returns_cached_singleton(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "s" * 40)
    f.reset_agent_wallet_cache()
    w1 = f.get_agent_wallet()
    w2 = f.get_agent_wallet()
    assert w1 is not None and w1 is w2


def test_standalone_gate_uses_durable_sink(monkeypatch, tmp_path):
    """M3: wallet DISABLED -> the standalone PolicyGate (DB-credential trading caps)
    must still get a durable audit sink so rolling-24h caps + replay survive a
    restart. Previously it was a plain in-memory list that reset every process."""
    from core.wallet.audit_sink import JsonlAuditSink

    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WALLET_DAILY_CAP_USD", "50")
    f.reset_agent_wallet_cache()

    assert f.get_agent_wallet() is None  # wallet disabled -> standalone path
    gate = f.get_policy_gate()
    assert isinstance(gate._audit, JsonlAuditSink)  # durable, not a plain list

    gate.record(venue="hyperliquid", action="order", amount_usd=6.0,
                counterparty="x", idempotency_key="m3", result_ref="0x")
    # a fresh process (cache reset) rebuilds the gate from the durable sink
    f.reset_agent_wallet_cache()
    gate2 = f.get_policy_gate()
    assert any(e.get("idempotency_key") == "m3" for e in gate2.audit_log)
