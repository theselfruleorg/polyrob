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
