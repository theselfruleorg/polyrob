"""W2.2 (x402 self-contained rail, 2026-08-21): the advertisement surfaces
(A2A agent card, ERC-8004 registration file) source their payment address
from the SAME `resolve_treasury_address()` resolver invoices use — so the
card and the invoices can never disagree, and the W1.1 wallet fallback
reaches the ads too."""
import pytest

WALLET_ADDR = "0xWa11etTreasury00000000000000000000000001"


class _FakeWallet:
    address = WALLET_ADDR


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for var in ("X402_PAYMENT_RECIPIENT", "X402_PAYMENT_ADDRESS",
                "X402_TREASURY_FROM_WALLET", "EIP8004_AGENT_WALLET"):
        monkeypatch.delenv(var, raising=False)
    import core.wallet.factory as factory
    monkeypatch.setattr(factory, "get_agent_wallet", lambda: _FakeWallet())


def test_agent_card_payment_address_uses_resolver():
    from api.a2a.agent_card import build_agent_card
    card = build_agent_card(None)
    x402_block = card.pricing["authentication_options"]["x402"]
    assert x402_block["payment_address"] == WALLET_ADDR


def test_eip8004_registration_agent_wallet_uses_resolver():
    from modules.eip8004.registration import build_registration_file
    reg = build_registration_file("https://example.test")
    wallets = [e for e in reg.services if e.name == "agentWallet"]
    assert len(wallets) == 1
    assert wallets[0].endpoint.endswith(f":{WALLET_ADDR}")
