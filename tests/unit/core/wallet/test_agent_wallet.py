import pytest
from core.wallet.config import WalletConfig
from core.wallet.agent_wallet import AgentWallet, VENUES

SEED = "test-seed-" + "z" * 40


def _cfg(**kw):
    base = dict(enabled=True, backend="local_eoa", master_seed=SEED, network="testnet",
                max_per_tx_usd=1000.0, x402_client_enabled=True, x402_facilitator_url="http://f")
    base.update(kw)
    return WalletConfig(**base)


def test_requires_master_seed_when_enabled():
    with pytest.raises(ValueError):
        AgentWallet(_cfg(master_seed=None))


def test_per_venue_addresses_are_distinct_and_deterministic():
    w1 = AgentWallet(_cfg())
    w2 = AgentWallet(_cfg())
    addrs = {v: w1.signer_for(v).address for v in VENUES}
    # distinct per venue
    assert len(set(addrs.values())) == len(VENUES)
    # deterministic across instances with same seed
    for v in VENUES:
        assert w1.signer_for(v).address == w2.signer_for(v).address


def test_treasury_address_property_matches_signer():
    w = AgentWallet(_cfg())
    assert w.address == w.signer_for("treasury").address


def test_unknown_venue_raises():
    w = AgentWallet(_cfg())
    with pytest.raises(ValueError):
        w.signer_for("nope")


def test_account_for_returns_local_account_with_matching_address():
    w = AgentWallet(_cfg())
    acct = w.account_for("hyperliquid")
    assert acct.address == w.signer_for("hyperliquid").address
