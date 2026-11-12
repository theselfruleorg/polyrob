"""064 S1 C0 (E-S1-00) — every chain-registry row is present in, or EXCUSED
from, every per-chain side table; and every side-table key is a registry row
or a named extra.

Six side tables repeat the registry and drifted (robinhood missing from the NFT
hosts, ERC-8004 and the Alchemy network map; optimism in two tables with no
registry row). A build session adding a chain now gets a failing checklist
instead of a half-added chain.

The excuse dicts live HERE, in the test: they are the audit trail, like the
surface parity matrix. Each excuse is a visible debt row in
the extension map (E-S1-01 Optimism P, E-S1-12 Robinhood closed 2026-11-08,
E-S1-13 Polygon + Arbitrum LP). Removing a debt = adding the chain to the side
table AND deleting its excuse; the test fails if an excuse outlives its debt.

⚠️ This test imports ``tools.defi.nft_verbs`` — a test may; the layering
ratchet is on production imports. ``core/`` stays free of that import.
"""
import pytest

from core.wallet import chains, dex_registry, erc8004, onchain
from core.payments import assets as payment_assets

REGISTRY = {row.name for row in chains.evm_rows()}
ALL_ROWS = {row.name for row in chains.all_rows()}

#: Side-table keys that are NOT registry rows, on purpose.
TESTNETS = {"ethereum-sepolia", "base-sepolia", "optimism-sepolia",
            "arbitrum-sepolia", "polygon-amoy"}
#: robinhood-testnet (46630) is a READ-ONLY registry row since 2026-09-29 (core handoff W13):
#: the agent-NFT collection rehearses there first. Tables with no testnet data excuse it.
_TESTNET_ROW = "robinhood-testnet: a read-only testnet row (W13) — no Alchemy/NFT host/LP rows for 46630"

EXCUSED_ALCHEMY = {
    "robinhood-testnet": _TESTNET_ROW,
}
EXCUSED_NFT = {
    "robinhood-testnet": _TESTNET_ROW,
}
EXCUSED_8004 = {
    "bnb": "ERC-8004 registry not checked on BSC — stage P (order 0015 is stage R)",
}  # robinhood pinned 2026-09-23 (measured; 050 §7.1 Phase 0)
EXCUSED_DEX = {
    "bnb": "stage R only; PancakeSwap LP rows are stage P",
    "optimism": "stage R only; LP rows are stage P — E-S1-01",
    "robinhood-testnet": _TESTNET_ROW,
}


def _nft_hosts():
    from tools.defi import nft_verbs
    return nft_verbs._ALCHEMY_HOSTS


SIDE_TABLES = {
    "alchemy_network": (lambda: set(onchain._ALCHEMY_NETWORK), EXCUSED_ALCHEMY),
    "nft_hosts": (lambda: set(_nft_hosts()), EXCUSED_NFT),
    "erc8004": (lambda: set(erc8004._ROWS) - TESTNETS, EXCUSED_8004),
    "dex_registry": (lambda: {r.chain for r in dex_registry.all_rows()}, EXCUSED_DEX),
}


@pytest.mark.parametrize("table", sorted(SIDE_TABLES))
def test_every_registry_row_is_present_or_excused(table):
    keys_fn, excused = SIDE_TABLES[table]
    keys = keys_fn()
    missing = sorted(REGISTRY - keys - set(excused))
    assert not missing, (
        f"{table}: registry rows {missing} are neither in the table nor excused. "
        f"Add the chain to the table (measured, never from memory) or add an excuse "
        f"with a reason and an extension-map debt row.")


@pytest.mark.parametrize("table", sorted(SIDE_TABLES))
def test_an_excuse_never_outlives_its_debt(table):
    keys_fn, excused = SIDE_TABLES[table]
    stale = sorted(set(excused) & keys_fn())
    assert not stale, f"{table}: {stale} are in the table now — delete their excuses"
    unknown = sorted(set(excused) - REGISTRY)
    assert not unknown, f"{table}: excuses for chains that are not registry rows: {unknown}"


@pytest.mark.parametrize("table", sorted(SIDE_TABLES))
def test_every_side_table_key_is_a_registry_row(table):
    keys_fn, _ = SIDE_TABLES[table]
    extra = sorted(keys_fn() - REGISTRY)
    assert not extra, f"{table}: keys {extra} have no chain-registry row"


def test_the_erc8004_testnets_are_the_named_extras():
    assert set(erc8004._ROWS) - REGISTRY == TESTNETS


def test_venues_and_payment_assets_name_only_registry_chains():
    """Venue and asset tables are not per-chain completeness tables: only the
    chains they NAME must be registry rows (a testnet asset may name its testnet)."""
    assert set(onchain.VENUE_CHAIN.values()) <= REGISTRY
    named = {a.chain for a in payment_assets.BUILTIN_ASSETS.values()}
    assert named - TESTNETS <= ALL_ROWS


def test_the_new_optimism_row_is_read_only():
    """064 D2 (owner): arming money on a new chain stays OFF by default."""
    row = chains.get("optimism")
    assert row is not None and row.chain_id == 10
    assert row.money_enabled is False
    # Order 0014: the route is filled; only the owner flip is missing.
    assert row.aggregator_spender == chains._LIFI_DIAMOND
    assert row.univ3_router and row.univ3_quoter
    assert chains.swap_ready("optimism") == (True, "")
    assert "optimism" not in chains.money_chains()
    assert "optimism" not in chains.swap_chains()
    ok, why = chains.money_capable("optimism")
    assert ok is False and "read-only" in why
    ok, why = chains.money_ready("optimism")
    assert ok is False and why


def test_robinhood_alchemy_rows_are_the_measured_ones():
    """E-S1-12 evidence (2026-11-08): ``robinhood-mainnet.g.alchemy.com`` served
    ``/nft/v3/.../getNFTsForOwner`` with a contract whose ``eth_getCode`` on the
    Robinhood public RPC (chain 4663) is non-empty; the host does not exist for a
    made-up slug. The RPC row shares the slug (the demo key 429s eth_chainId)."""
    assert onchain._ALCHEMY_NETWORK["robinhood"] == "robinhood-mainnet"
    assert _nft_hosts()["robinhood"] == "robinhood-mainnet.g.alchemy.com"


def test_bnb_usdc_has_18_decimals_in_every_reader(monkeypatch):
    """BNB Chain's USDC is Binance-peg with 18 decimals. The canonical pin said 6
    with `verified`, so a balance read a million million times too large."""
    from core.wallet import chains, onchain, tokens
    row = chains.get("bnb")
    assert row.usdc_decimals == 18
    assert tokens.CANONICAL_TOKENS[("bnb", row.usdc)]["decimals"] == 18
    one_and_a_half = hex(15 * 10 ** 17)
    monkeypatch.setattr(onchain, "_rpc", lambda rpc, method, params, timeout: (
        "0x0" if method == "eth_getBalance" else one_and_a_half))
    assert onchain.balances("0x" + "11" * 20, "bnb")[1] == 1.5


def test_usdc_has_6_decimals_on_every_other_chain():
    from core.wallet import chains, tokens
    for row in chains.all_rows():
        if row.name != "bnb" and row.usdc:
            assert row.usdc_decimals == 6, row.name
            pin = tokens.CANONICAL_TOKENS.get((row.name, row.usdc))
            assert pin is None or pin["decimals"] == 6, row.name


def test_bnb_row_is_pinned_and_read_only():
    """Order 0015: measured 2026-11-11 on two RPCs; the USDC is the 18-decimal peg."""
    row = chains.get("bnb")
    assert row is not None and row.chain_id == 56 and row.native_symbol == "BNB"
    assert row.usdc == "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
    assert row.wrapped_native == "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
    assert (row.dexscreener_id, row.geckoterminal_id, row.goplus_id) == ("bsc", "bsc", "56")
    assert row.money_enabled is False and row.aggregator_spender is None
    assert "bnb" not in chains.money_chains() and "bnb" not in chains.swap_chains()
    ok, why = chains.money_capable("bnb")
    assert ok is False and why
    assert onchain._ALCHEMY_NETWORK["bnb"] == "bnb-mainnet"
    assert _nft_hosts()["bnb"] == "bnb-mainnet.g.alchemy.com"
