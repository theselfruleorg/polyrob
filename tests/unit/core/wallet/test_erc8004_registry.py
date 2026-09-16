"""046 step 2: the ERC-8004 registries are PINNED, not configured.

⚠️ An address in a config file is a claim, not a check. `tools/launchpad/pons.py`
already learned this: it re-verifies its factory by code hash on every call and
refuses on mismatch, because web search hands you the WRONG generation of a
protocol (it returns Pons V1, a different protocol, for the V2 factory).

The ERC-8004 reference registries are per-chain singletons deployed at the same
vanity address on every mainnet, so pinning them in code is both possible and
correct. `EIP8004_*_REGISTRY` env may only NARROW to a pinned row — it can never
introduce an arbitrary address, because "register me at this contract" supplied
from outside is a call to an arbitrary contract wearing a registration's name.

Addresses verified 2026-09-15 against the erc-8004/erc-8004-contracts
deployments list.
"""
import pytest

from core.wallet import erc8004


IDENTITY_MAINNET = "0x8004a169fb4a3325136eb29fa0ceb6d2e539a432"
REPUTATION_MAINNET = "0x8004baa17c55a88189ae136b182e5fda19de9b63"
IDENTITY_TESTNET = "0x8004a818bfb912233c491871b3d84c89a494bd9e"


def test_the_money_chains_all_have_a_pinned_identity_registry():
    """Every chain the wallet can spend on can also carry the agent's identity —
    otherwise the identity lives somewhere the agent cannot act."""
    from core.wallet import chains
    for name in chains.money_chains():
        row = erc8004.registry_for(name)
        if row is None:
            continue  # a chain with no ERC-8004 deployment is allowed to say so
        assert row.identity.startswith("0x") and len(row.identity) == 42, name


def test_base_is_pinned_to_the_published_singleton():
    row = erc8004.registry_for("base")
    assert row is not None
    assert row.identity.lower() == IDENTITY_MAINNET
    assert row.reputation.lower() == REPUTATION_MAINNET


def test_ethereum_shares_the_same_singleton_address():
    assert erc8004.registry_for("ethereum").identity.lower() == IDENTITY_MAINNET


def test_a_testnet_has_its_own_distinct_address():
    """⚠️ Not the mainnet one. Registering on a testnet at a mainnet address
    would silently call whatever happens to live there."""
    row = erc8004.registry_for("base-sepolia")
    assert row is not None
    assert row.identity.lower() == IDENTITY_TESTNET
    assert row.identity.lower() != IDENTITY_MAINNET


def test_an_unknown_chain_returns_none_rather_than_guessing():
    assert erc8004.registry_for("dogecoin") is None
    assert erc8004.registry_for("") is None


# --- the env may only NARROW ----------------------------------------------

def test_a_matching_env_override_is_accepted(monkeypatch):
    monkeypatch.setenv("EIP8004_IDENTITY_REGISTRY", IDENTITY_MAINNET.upper())
    assert erc8004.resolve_identity_registry("base").lower() == IDENTITY_MAINNET


def test_an_arbitrary_env_address_is_REFUSED(monkeypatch):
    """⚠️ The load-bearing rule. Without it, one env var turns `register_agent`
    into `call an arbitrary contract from the treasury wallet`."""
    monkeypatch.setenv("EIP8004_IDENTITY_REGISTRY", "0x" + "de" * 20)
    with pytest.raises(ValueError) as exc:
        erc8004.resolve_identity_registry("base")
    assert "pinned" in str(exc.value).lower()


def test_no_env_resolves_to_the_pinned_address(monkeypatch):
    monkeypatch.delenv("EIP8004_IDENTITY_REGISTRY", raising=False)
    assert erc8004.resolve_identity_registry("base").lower() == IDENTITY_MAINNET


def test_an_unsupported_chain_refuses_with_its_reason(monkeypatch):
    monkeypatch.delenv("EIP8004_IDENTITY_REGISTRY", raising=False)
    with pytest.raises(ValueError) as exc:
        erc8004.resolve_identity_registry("dogecoin")
    assert "dogecoin" in str(exc.value)


# --- the addresses are real, checksummed, distinct -------------------------

def test_identity_and_reputation_are_never_the_same_address():
    for name in erc8004.supported_chains():
        row = erc8004.registry_for(name)
        assert row.identity.lower() != row.reputation.lower(), name


def test_every_pinned_address_is_a_valid_20_byte_address():
    for name in erc8004.supported_chains():
        row = erc8004.registry_for(name)
        for addr in (row.identity, row.reputation):
            assert addr.startswith("0x") and len(addr) == 42, (name, addr)
            int(addr, 16)  # raises if it is not hex


def test_the_chain_id_matches_the_wallet_chain_registry():
    """One chain vocabulary. A registry row whose chain_id disagrees with
    core/wallet/chains.py would register on a different chain than the one the
    wallet signs for."""
    from core.wallet import chains
    for name in erc8004.supported_chains():
        row = chains.get(name)
        if row is None:
            continue
        assert erc8004.registry_for(name).chain_id == row.chain_id, name
