"""CR info items: private keys never appear in a repr."""

KEY = "0x" + "ab" * 32


def test_hyperliquid_models_hide_private_keys():
    import pytest
    models = pytest.importorskip("polyrob_markets.hyperliquid.models")  # the markets pack
    AgentWallet, HyperliquidCredentials = models.AgentWallet, models.HyperliquidCredentials
    aw = AgentWallet(address="0x1", private_key=KEY)
    creds = HyperliquidCredentials(user_id="u", wallet_address="0x2",
                                   private_key=KEY, agent_wallet=aw)
    assert KEY not in repr(aw)
    assert KEY not in repr(creds)
    assert creds.private_key == KEY


def test_botconfig_never_holds_the_eip8004_key(monkeypatch):
    # 067 F4: the unread field is gone — modules/eip8004 reads the env directly,
    # so the key cannot reach a BotConfig repr or dump at all.
    from core.config import BotConfig
    monkeypatch.setenv("EIP8004_AGENT_PRIVATE_KEY", KEY)
    cfg = BotConfig()
    assert KEY not in repr(cfg)
    assert KEY not in str(cfg.model_dump())
    assert "eip8004_agent_private_key" not in BotConfig.model_fields
