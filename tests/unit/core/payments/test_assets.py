"""The payment-asset registry: one row per asset, no default row (046 Phase 0)."""
import pytest

from core.payments import assets


def test_builtin_usdc_base_matches_the_constant_the_scan_already_trusts():
    from core.wallet.onchain import USDC_BASE_MAINNET
    row = assets.get("usdc-base")
    assert row is not None
    assert row.chain == "base"
    assert row.address.lower() == USDC_BASE_MAINNET.lower()
    assert row.decimals == 6
    assert row.symbol == "USDC"
    assert row.rail == "facilitator"


def test_builtin_usdc_base_sepolia_is_its_own_row():
    from core.wallet.onchain import USDC_BASE_SEPOLIA
    row = assets.get("usdc-base-sepolia")
    assert row is not None
    assert row.chain == "base-sepolia"
    assert row.address.lower() == USDC_BASE_SEPOLIA.lower()


def test_an_unknown_asset_resolves_to_none_and_never_to_a_default():
    assert assets.get("no-such-asset") is None
    assert assets.get("") is None
    assert assets.get(None) is None


def test_known_ids_names_the_vocabulary_a_refusal_can_echo():
    ids = assets.known_ids()
    assert "usdc-base" in ids
    assert "usdc-base-sepolia" in ids


def test_every_mainnet_builtin_names_a_chain_the_wallet_registry_knows():
    """An asset on a chain `core/wallet/chains.py` has no row for is
    unscannable — there is no pinned RPC and no pinned chain id for it.

    ⚠️ `base-sepolia` is the ONE exemption and it is explicit: the money chain
    registry deliberately carries only chains whose addresses were verified
    on-chain for TRADING, and putting a testnet into `all_rows()` would place
    it in front of every DeFi surface that iterates it. The x402 module already
    owns the sepolia RPC and chain id (`modules/x402/artifact.py`), so the scan
    resolves that one itself.
    """
    from core.wallet import chains
    for row in assets.BUILTIN_ASSETS.values():
        if row.asset_id in assets.TESTNET_EXEMPT_ASSET_IDS:
            continue
        assert chains.get(row.chain) is not None, (
            f"{row.asset_id} names chain {row.chain!r}, which core/wallet/chains.py "
            f"does not have a row for — an asset on an unknown chain is unscannable")


def test_the_testnet_exemption_is_a_closed_set_not_a_loophole():
    """Only the sepolia row may skip the chain-registry check. A new asset
    cannot be exempted by putting it on an unknown chain."""
    assert assets.TESTNET_EXEMPT_ASSET_IDS == frozenset({"usdc-base-sepolia"})


def test_the_row_is_frozen_so_a_caller_cannot_mutate_decimals():
    row = assets.get("usdc-base")
    with pytest.raises(Exception):
        row.decimals = 18


def test_a_builtin_row_is_marked_builtin():
    assert assets.get("usdc-base").source == "builtin"


def test_the_default_asset_id_is_the_one_every_pre_046_caller_meant():
    assert assets.DEFAULT_ASSET_ID == "usdc-base"
    assert assets.get(assets.DEFAULT_ASSET_ID) is not None


def test_the_solana_mints_agree_with_the_wallets_own_pins():
    """⚠️ The mints are COPIED here, not imported (this module stays free of the
    x402 SDK import `solana_x402` carries). This is the test that keeps the two
    copies from drifting — a wrong mint quotes a payer an address nobody holds."""
    from core.wallet.solana_x402 import _USDC_MINTS
    assert assets.get("usdc-solana").address == _USDC_MINTS["mainnet"]
    assert assets.get("usdc-solana-devnet").address == _USDC_MINTS["testnet"]


def test_the_two_solana_rows_are_distinct_mints():
    """Devnet USDC is a DIFFERENT mint; conflating them is a silent
    misdirection of real money onto a test network, or the reverse."""
    assert (assets.get("usdc-solana").address
            != assets.get("usdc-solana-devnet").address)


def test_solana_default_picks_devnet_unless_the_wallet_says_mainnet():
    """⚠️ Fail toward the TEST network. A misread that reached for mainnet USDC
    would quote a payer the real-money mint on a test run."""
    assert assets.solana_default_asset_id("mainnet") == "usdc-solana"
    assert assets.solana_default_asset_id("testnet") == "usdc-solana-devnet"
    assert assets.solana_default_asset_id("nonsense") == "usdc-solana-devnet"


def test_the_solana_rows_use_their_own_rail():
    """The Solana settlement pass matches by unique REFERENCE, not by amount.
    Naming that rail keeps EVM amount-matching reasoning off it."""
    assert assets.get("usdc-solana").rail == "svm_reference"
    assert "svm_reference" in assets.RAILS
