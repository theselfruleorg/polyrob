"""A genuine owner turn that transfers to an address that is OURS (the owner's
configured OWNER_WALLET_ADDRESSES, or the agent's own wallet) waits on no
second owner tap. Attack paths kept closed: a third-party recipient, a forged
or autonomous turn, a lookalike of our address, and every non-transfer verb
still go to the owner queue."""
from types import SimpleNamespace

import pytest

from tools.controller import self_transfer as st

OWNER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
AGENT = "0x2222222222222222222222222222222222222222"
SOL = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
MALLORY = "0x3333333333333333333333333333333333333333"
LOOKALIKE = "0x70997970C51812dc3A010C7d01b50e0d17dc7999"


@pytest.fixture
def ours(monkeypatch):
    monkeypatch.setenv("OWNER_WALLET_ADDRESSES", f" {OWNER.lower()} , ")
    from core.wallet import factory
    monkeypatch.setattr(factory, "get_agent_wallet",
                        lambda: SimpleNamespace(address=AGENT, solana_address=SOL))
    monkeypatch.setattr("core.security.owner_turn.is_genuine_owner_turn",
                        lambda ctx: bool(ctx.genuine))


GENUINE = SimpleNamespace(genuine=True)
FORGED = SimpleNamespace(genuine=False)


def _ex(action, to, ctx=GENUINE):
    return st.spend_exemption_with_turn(action, {"to": to, "dry_run": False,
                                                 "max_spend_usd": 5000.0}, ctx)


def test_owner_turn_to_our_addresses_needs_no_tap(ours):
    assert "owner's configured" in _ex("defi_trade_transfer", OWNER)
    assert "agent's own wallet" in _ex("defi_trade_transfer", AGENT)
    assert "agent's own wallet" in _ex("defi_trade_solana_transfer", SOL)


def test_third_party_forged_lookalike_and_other_verbs_still_queue(ours):
    assert _ex("defi_trade_transfer", MALLORY) is None
    assert _ex("defi_trade_transfer", LOOKALIKE) is None
    assert _ex("defi_trade_transfer", OWNER, FORGED) is None
    assert _ex("defi_trade_transfer", OWNER, None) is None
    assert _ex("defi_trade_swap", OWNER) is None
    assert _ex("defi_trade_nft_transfer", OWNER) is None
    # base58 is case-sensitive: a case-folded Solana address is another account
    assert _ex("defi_trade_solana_transfer", SOL.lower()) is None


def test_the_hook_takes_context_and_the_list_is_console_unwritable():
    from core.config_service import is_console_unwritable
    assert st.spend_exemption_with_turn.takes_context is True
    assert is_console_unwritable("OWNER_WALLET_ADDRESSES")
