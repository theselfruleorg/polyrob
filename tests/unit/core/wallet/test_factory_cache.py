"""098 CO-10: a disabled agent wallet is not cached for the life of the process."""
from types import SimpleNamespace

from core.wallet import factory


def test_enable_after_disabled_resolves_after_reset_or_recheck(monkeypatch):
    factory.reset_agent_wallet_cache()
    cfg = {"enabled": False}
    built = []

    class _W:
        def __init__(self, c, audit_sink=None, on_record=None):
            built.append(self)

    monkeypatch.setattr(factory, "load_wallet_config",
                        lambda: SimpleNamespace(enabled=cfg["enabled"], master_seed="s" * 64))
    monkeypatch.setattr(factory, "_signer_mode", lambda: "local")
    monkeypatch.setattr(factory, "_durable_audit_sink", lambda: None)
    monkeypatch.setattr(factory, "AgentWallet", _W)
    import core.wallet.public_identity as pi
    monkeypatch.setattr(pi, "maybe_publish", lambda w: None)
    clock = [1000.0]
    monkeypatch.setattr(factory.time, "monotonic", lambda: clock[0])
    try:
        assert factory.get_agent_wallet() is None
        cfg["enabled"] = True  # `wallet init` in a running process
        assert factory.get_agent_wallet() is None  # inside the recheck window
        clock[0] += factory.WALLET_DISABLED_RECHECK_SEC
        w = factory.get_agent_wallet()
        assert isinstance(w, _W) and factory.get_agent_wallet() is w and len(built) == 1
        factory.reset_agent_wallet_cache()
        cfg["enabled"] = False
        assert factory.get_agent_wallet() is None
        cfg["enabled"] = True
        factory.reset_agent_wallet_cache()  # the owner seat may reset at once
        assert isinstance(factory.get_agent_wallet(), _W)
    finally:
        factory.reset_agent_wallet_cache()
