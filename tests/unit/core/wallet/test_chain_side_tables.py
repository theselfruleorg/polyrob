"""064 S1 C0 (E-S1-00) — every chain-registry row is present in, or EXCUSED
from, every per-chain side table; and every side-table key is a registry row
or a named extra.

Six side tables repeat the registry and drifted (robinhood missing from the NFT
hosts, ERC-8004 and the Alchemy network map; optimism in two tables with no
registry row). A build session adding a chain now gets a failing checklist
instead of a half-added chain.

The excuse dicts live HERE, in the test: they are the audit trail, like the
surface parity matrix. Each excuse is a visible debt row in
the extension map (E-S1-01 Optimism P, E-S1-12 Robinhood P,
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
    "robinhood": "no Alchemy network for Robinhood Chain (checked 2026-09-23) — E-S1-12",
    "robinhood-testnet": _TESTNET_ROW,
}
EXCUSED_NFT = {
    "robinhood": "no Alchemy NFT host for Robinhood Chain (checked 2026-09-23) — E-S1-12",
    "robinhood-testnet": _TESTNET_ROW,
}
EXCUSED_8004: dict = {}  # robinhood pinned 2026-09-23 (measured; 050 §7.1 Phase 0)
EXCUSED_DEX = {
    "arbitrum": "no V3/V4 position-manager rows pinned for LP — E-S1-13",
    "polygon": "no V3/V4 position-manager rows pinned for LP — E-S1-13",
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
    assert row.money_enabled is False and row.route_hints == ()
    assert row.aggregator_spender is None and row.univ3_router is None
    assert "optimism" not in chains.money_chains()
    assert "optimism" not in chains.swap_chains()
    ok, why = chains.money_ready("optimism")
    assert ok is False and why
