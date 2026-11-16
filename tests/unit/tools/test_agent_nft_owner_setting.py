"""AGENT_NFT_ENABLED is an owner setting (Access `console`): the agent proposes it on a
one-tap card, and the off refusal says so instead of sending it to the operator."""
from core import config_service
from core.setting_proposals import CARD, classify


def test_agent_nft_flag_is_remote_writable():
    assert config_service.remote_write_refused("AGENT_NFT_ENABLED", "true") is False


def test_the_agent_can_propose_it_on_a_card():
    p = classify("/config set AGENT_NFT_ENABLED true")
    assert p.outcome == CARD
    assert p.words == ["/config", "set", "AGENT_NFT_ENABLED", "true"]
    assert p.when == "restart"


def test_account_mode_off_refusal_names_propose_setting(monkeypatch):
    monkeypatch.delenv("AGENT_NFT_ENABLED", raising=False)
    from core.remedy import owner_flag_remedy
    text = owner_flag_remedy("AGENT_NFT_ENABLED")
    assert "propose_setting(command=\"/config set AGENT_NFT_ENABLED true\")" in text
    assert "restart" in text
