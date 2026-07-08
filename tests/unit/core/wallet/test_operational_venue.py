"""The 'spend from treasury' fix (2026-07-08).

Regression guard for the fund-the-wrong-address footgun: the owner-facing address
(`AgentWallet.address`) MUST equal the address a same-chain spend actually signs with
(`operational_signer()`), so funding the surfaced address always funds the spend path.
"""
from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import load_wallet_config

_SEED = "x" * 48  # >=32 chars


def _wallet(env=None):
    e = {"AGENT_WALLET_ENABLED": "true", "AGENT_WALLET_MASTER_SEED": _SEED}
    e.update(env or {})
    return AgentWallet(load_wallet_config(env=e))


def test_operational_venue_defaults_to_treasury():
    assert load_wallet_config(env={}).operational_venue == "treasury"
    assert _wallet().operational_venue == "treasury"


def test_env_overrides_operational_venue():
    assert load_wallet_config(
        env={"AGENT_WALLET_OPERATIONAL_VENUE": "x402"}).operational_venue == "x402"


def test_address_equals_operational_signer_INVARIANT():
    """The core guard: surfaced address == the address spent from."""
    for venue in ("treasury", "x402"):
        w = _wallet({"AGENT_WALLET_OPERATIONAL_VENUE": venue})
        assert w.address == w.operational_signer().address
        assert w.address == w.signer_for(venue).address


def test_default_spends_from_treasury_not_x402():
    """Default: the agent spends from treasury, and treasury != the old x402 venue key."""
    w = _wallet()
    assert w.operational_signer().address == w.signer_for("treasury").address
    assert w.signer_for("treasury").address != w.signer_for("x402").address


def test_unknown_operational_venue_falls_back_to_treasury():
    w = _wallet({"AGENT_WALLET_OPERATIONAL_VENUE": "bogus"})
    assert w.operational_venue == "treasury"
    assert w.address == w.signer_for("treasury").address


def test_delegated_venues_are_clamped_out_of_operational():
    """hyperliquid/polymarket derived keys never hold a spendable float — pointing the
    operational venue at them must fall back to treasury, not strand funds."""
    for venue in ("hyperliquid", "polymarket"):
        w = _wallet({"AGENT_WALLET_OPERATIONAL_VENUE": venue})
        assert w.operational_venue == "treasury"
        assert w.address == w.signer_for("treasury").address


def test_config_and_network_introspectable():
    w = _wallet({"AGENT_WALLET_NETWORK": "mainnet"})
    assert w.config is not None
    assert w.network == "mainnet"
