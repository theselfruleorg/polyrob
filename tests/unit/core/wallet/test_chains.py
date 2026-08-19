"""The chain registry — ONE table every chain-aware seam reads.

Before this existed, "base" was spread across the rail, the token pins, the
univ3 addresses and four providers, each with its own idea of what a chain is.
The rules these tests pin:

* a chain's values come from ITS OWN row, never another chain's (a wrong router
  does not error — it sends funds nowhere);
* a missing capability degrades HONESTLY (a named refusal), never by silently
  falling back to a chain that does have it;
* chain ids are CONFIG, pinned here, never read from an RPC.

Every address asserted below was verified on-chain (eth_getCode non-empty, plus
symbol/decimals for tokens) on 2026-08-17.
"""
import pytest

from core.wallet import chains


def test_every_row_pins_a_unique_chain_id():
    ids = [row.chain_id for row in chains.all_rows()]
    assert len(ids) == len(set(ids)), "two chains sharing an id would cross-sign"
    assert all(isinstance(i, int) and i > 0 for i in ids)


def test_an_unknown_chain_resolves_to_nothing_not_a_default():
    """A silent fallback to base is how a chain-X transaction gets signed with
    base state. Unknown must stay unknown."""
    assert chains.get("nosuchchain") is None


def test_the_base_row_carries_its_verified_addresses():
    row = chains.get("base")
    assert row.chain_id == 8453
    assert row.native_symbol == "ETH"
    assert row.usdc == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert row.univ3_router == "0x2626664c2603336E57B271c5C0b26F421741e481"
    assert row.univ3_quoter == "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    assert row.wrapped_native == "0x4200000000000000000000000000000000000006"
    assert row.dexscreener_id == "base"
    assert row.goplus_id == "8453"
    assert row.alchemy_slug == "base-mainnet"


def test_the_ethereum_row_carries_its_verified_addresses():
    """Ethereum's Uniswap V3 deployment is at DIFFERENT addresses from Base's.
    Verified 2026-08-17: USDC symbol=USDC/decimals=6, the quoter returns a real
    0.02 USDC -> WETH quote."""
    row = chains.get("ethereum")
    assert row.chain_id == 1
    assert row.native_symbol == "ETH"
    assert row.usdc == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    assert row.univ3_router == "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
    assert row.univ3_quoter == "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
    assert row.wrapped_native == "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
    assert row.dexscreener_id == "ethereum"
    assert row.goplus_id == "1"


def test_ethereum_and_base_never_share_an_address():
    """The whole failure mode this registry prevents: a Base router used on
    Ethereum sends funds nowhere and does not error."""
    eth, base = chains.get("ethereum"), chains.get("base")
    for field in ("usdc", "univ3_router", "univ3_quoter", "wrapped_native"):
        assert getattr(eth, field) != getattr(base, field), field


def test_robinhood_is_a_known_chain_with_no_dex():
    """Chain 4663 (Arbitrum Orbit, ETH gas) is real and readable, but no
    Uniswap V3 deployment verified there — so it is data-only."""
    row = chains.get("robinhood")
    assert row.chain_id == 4663
    assert row.native_symbol == "ETH"
    assert row.univ3_router is None
    assert row.univ3_quoter is None


def test_a_chain_without_a_dex_refuses_swaps_by_name():
    ok, why = chains.swap_ready("robinhood")
    assert ok is False
    assert "robinhood" in why
    assert "uniswap" in why.lower() or "dex" in why.lower()
    assert "base" not in why.lower(), "never point a refusal at another chain"


def test_a_chain_with_a_verified_dex_is_swap_ready():
    for name in ("base", "ethereum"):
        ok, why = chains.swap_ready(name)
        assert ok is True, (name, why)


def test_swap_ready_refuses_an_unknown_chain():
    ok, why = chains.swap_ready("nosuchchain")
    assert ok is False
    assert "nosuchchain" in why


def test_money_requires_a_pinned_rpc_and_names_the_env_var(monkeypatch):
    """The simulation, the deltas and the caps all read from the endpoint, so
    an unpinned chain cannot be the trust anchor for moving funds."""
    monkeypatch.delenv("DEFI_EVM_RPC_ETHEREUM", raising=False)
    ok, why = chains.money_ready("ethereum")
    assert ok is False
    assert "DEFI_EVM_RPC_ETHEREUM" in why


def test_money_ready_with_a_pinned_rpc(monkeypatch):
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", "https://pinned.example/rpc")
    ok, why = chains.money_ready("base")
    assert ok is True, why


def test_a_data_only_chain_refuses_money_even_when_pinned(monkeypatch):
    """Robinhood is readable and pinned on prod, but nothing about it has been
    proven for value movement — a pinned RPC alone must not arm it."""
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", "https://pinned.example/rpc")
    ok, why = chains.money_ready("robinhood")
    assert ok is False
    assert "robinhood" in why


def test_money_chains_are_a_subset_of_known_chains():
    known = {row.name for row in chains.all_rows()}
    assert set(chains.money_chains()) <= known
    assert "base" in chains.money_chains()


def test_every_row_explains_what_the_chain_is_for():
    """The agent picks the chain per task (owner directive 2026-08-15), so each
    row must carry guidance it can actually read."""
    for row in chains.all_rows():
        assert row.purpose and len(row.purpose) > 20, row.name


def test_the_fee_ceiling_is_per_chain_not_one_l2_number():
    """One L2-sized ceiling refused every honest L1 transaction; L1 gas is an
    order of magnitude above an L2's."""
    assert chains.get("ethereum").max_fee_wei_per_tx > chains.get("base").max_fee_wei_per_tx


@pytest.mark.parametrize("name", ["base", "ethereum", "robinhood"])
def test_rpc_env_var_follows_the_documented_convention(name):
    assert chains.get(name).rpc_env == f"DEFI_EVM_RPC_{name.upper()}"
