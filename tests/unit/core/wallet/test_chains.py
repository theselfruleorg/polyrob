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


def test_every_evm_row_pins_a_unique_chain_id():
    ids = [row.chain_id for row in chains.evm_rows()]
    assert len(ids) == len(set(ids)), "two chains sharing an id would cross-sign"
    assert all(isinstance(i, int) and i > 0 for i in ids)


def test_a_non_evm_row_carries_no_chain_id_at_all():
    """chain_id is EIP-155 and has no analogue off EVM. It is 0 rather than a
    made-up number, and 0 is load-bearing: it is falsey, and
    signer.sign_transaction refuses a transaction with no chainId, so a non-EVM
    row cannot be signed through the EVM rail even if a caller reached one."""
    for row in chains.all_rows():
        if row.family != "evm":
            assert row.chain_id == 0, row.name


def test_every_row_declares_a_family():
    for row in chains.all_rows():
        assert row.family in ("evm", "svm"), row.name


def test_a_non_evm_row_cannot_be_money_enabled_without_a_rail():
    """Nothing off EVM has a signer, a broadcast rail or a simulation, so
    money_enabled there would be a claim no code can honour."""
    for row in chains.all_rows():
        if row.family != "evm":
            assert row.money_enabled is False, row.name
            assert row.route_hints == (), row.name


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
    import re
    ok, why = chains.swap_ready("solana")
    assert ok is False
    assert "solana" in why
    assert "uniswap" in why.lower() or "dex" in why.lower()
    # Never point a refusal at ANOTHER chain. Word-boundary matched: Solana's
    # own purpose text legitimately contains "base58", and a naive substring
    # check read that as the Base chain.
    others = {r.name for r in chains.all_rows()} - {"solana"}
    for other in others:
        assert not re.search(rf"\b{other}\b", why.lower()), (
            f"refusal for solana mentions {other}")


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
    """Solana is readable and pinned on prod, but nothing about it has been
    proven for value movement — a pinned RPC alone must not arm it."""
    monkeypatch.setenv("DEFI_EVM_RPC_ROBINHOOD", "https://pinned.example/rpc")
    ok, why = chains.money_ready("solana")
    assert ok is False
    assert "solana" in why


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


# --------------------------------------------------------------------------
# EVM chain extension (029 §4) — arming Arbitrum and Polygon
# --------------------------------------------------------------------------

def test_a_money_chain_can_actually_afford_a_transaction():
    """max_fee_wei_per_tx is an ANOMALY brake, not a budget — but a brake set
    below the real cost of ONE ordinary transaction is not a brake, it is a
    chain that silently refuses everything while looking armed.

    Gas prices measured 2026-08-25 for a 250k-gas transaction:
      ethereum  5.8 gwei  -> 0.00144 ETH  (cap 0.01  = ~6 tx)
      base      0.006 gwei-> 0.0000015 ETH(cap 0.002 = ~1300 tx)
      arbitrum  0.02 gwei -> 0.000005 ETH (cap 0.002 = ~400 tx)
      polygon   276 gwei  -> 0.069 POL    (cap 0.5   = ~7 tx)

    Polygon is the reason this test exists: it inherited the 0.002 default,
    which covered ZERO transactions there. The expectations below are ORDINARY
    conditions, deliberately not worst-case — Ethereum's brake is meant to bite
    during an L1 fee spike, and that is the feature.
    """
    typical_gas = 250_000
    ordinary_gwei = {"ethereum": 20.0, "base": 1.0, "arbitrum": 1.0,
                     "polygon": 400.0, "robinhood": 1.0}
    for row in chains.evm_rows():
        if not row.money_enabled:
            continue
        gwei = ordinary_gwei.get(row.name)
        assert gwei is not None, (
            f"{row.name} was armed without recording what its gas costs — "
            f"add a measured figure here before enabling money on a chain")
        need = int(gwei * 1e9) * typical_gas
        assert row.max_fee_wei_per_tx >= need, (
            f"{row.name}: ceiling {row.max_fee_wei_per_tx} cannot pay for one "
            f"{typical_gas}-gas tx at {gwei} gwei ({need}) — an armed chain "
            f"that refuses every transaction")


def test_arbitrum_and_polygon_are_swap_capable_through_the_aggregator():
    """Neither carries a verified Uniswap V3 deployment, so the aggregator is
    the whole route — which is exactly what 029 makes possible."""
    for name in ("arbitrum", "polygon"):
        row = chains.get(name)
        assert row.univ3_router is None and row.univ3_quoter is None
        assert row.aggregator_spender
        ok, why = chains.swap_ready(name)
        assert ok, why


def test_an_armed_chain_still_needs_a_pinned_rpc_before_it_can_move_value(monkeypatch):
    """money_enabled is capability; money_ready is capability AND trust anchor.
    Arming a chain must not be enough on its own."""
    for name in ("arbitrum", "polygon"):
        monkeypatch.delenv(f"DEFI_EVM_RPC_{name.upper()}", raising=False)
        ok, why = chains.money_ready(name)
        assert not ok
        assert f"DEFI_EVM_RPC_{name.upper()}" in why


def test_every_money_chain_pins_the_addresses_its_pins_table_needs():
    """At least one quote asset. USDC on most chains; Robinhood quotes in WETH
    and has no routable stablecoin, so demanding USDC of every money chain
    would refuse a chain that trades fine."""
    for row in chains.evm_rows():
        if not row.money_enabled:
            continue
        assert row.usdc or row.wrapped_native, row.name


def test_the_wrapped_native_pin_is_not_labelled_wrapped_ether_everywhere():
    """Polygon's wrapped native is WPOL, not WETH. A hardcoded 'Wrapped Ether'
    name would put a false identity on the canonical list — the one table whose
    whole value is that verified=True means something."""
    from core.wallet.tokens import CANONICAL_TOKENS as pins
    poly = chains.get("polygon")
    if poly.money_enabled:
        entry = pins[("polygon", poly.wrapped_native)]
        assert entry["symbol"] == "WPOL"
        assert "Ether" not in entry["name"]


# --------------------------------------------------------------------------
# Robinhood Chain, promoted from data-only (2026-08-25)
#
# The row said "no Uniswap V3 deployment and no independent price feed were
# verified here, so it is DATA-ONLY". Both halves went stale: DexScreener,
# GeckoTerminal AND GoPlus all index it now, and it is the #2 chain by paid
# attention on DexScreener's boost surface (13 entries vs Base's 0, measured
# 2026-08-25) with minutes-old pools and CASHCAT doing $35M/24h.
#
# The catch is the QUOTE ASSET: there is no routable stablecoin on it. Pairs
# quote in WETH, and LI.FI returns "no available quotes" for every stablecoin
# leg tried. So a money chain does not necessarily have USDC, and the registry
# had quietly assumed it did.
# --------------------------------------------------------------------------

def test_robinhood_is_screenable_by_every_provider_that_indexes_it():
    row = chains.get("robinhood")
    assert row.dexscreener_id and row.geckoterminal_id and row.goplus_id


def test_robinhood_routes_through_the_aggregator_only():
    """No Uniswap V3 router is pinned there, so `lifi` is the whole route — the
    case 029's provider seam was built for."""
    row = chains.get("robinhood")
    assert row.univ3_router is None
    assert row.route_hints == ("lifi",)
    ok, why = chains.swap_ready("robinhood")
    assert ok, why


def test_robinhood_pins_its_OWN_aggregator_spender():
    """The LI.FI Diamond that every other chain uses has NO CODE on Robinhood —
    a borrowed address here would send funds to empty space, which is exactly
    what the per-chain pin exists to prevent."""
    rh = chains.get("robinhood").aggregator_spender
    base = chains.get("base").aggregator_spender
    assert rh and rh.lower() != base.lower()


def test_a_money_chain_needs_a_quote_asset_but_not_necessarily_usdc():
    """Robinhood quotes in WETH and has no routable stablecoin. Demanding USDC
    of every money chain would have refused a chain that trades fine."""
    for row in chains.evm_rows():
        if not row.money_enabled:
            continue
        assert row.usdc or row.wrapped_native, (
            f"{row.name} is money-enabled with no quote asset at all")


def test_robinhoods_quote_asset_is_its_wrapped_native():
    row = chains.get("robinhood")
    assert row.wrapped_native
    assert row.usdc is None, "no routable stablecoin was found there"


def test_the_purpose_no_longer_claims_it_is_data_only():
    p = chains.get("robinhood").purpose.lower()
    assert "data-only" not in p and "data only" not in p
    assert "weth" in p or "no stablecoin" in p, "the quote-asset catch must be stated"


def test_a_chain_with_its_own_rail_is_not_told_nothing_can_move_on_it():
    """money_capable() gates the EVM money verbs. Its refusal ended with
    "Nothing can be sent, approved or swapped on it" for EVERY non-money row —
    true for robinhood (no rail at all), false for solana, which has its own
    signer, simulation and swap verb. The message contradicted itself in the
    same breath (it names solana_swap, then denies any swap is possible), and a
    self-contradicting refusal is how the agent concluded Solana was unusable.
    """
    from core.wallet import chains
    ok, why = chains.money_capable("solana")
    assert ok is False, "the EVM rail must still refuse solana"
    assert "solana_swap" in why, "the refusal must name the verb that DOES work"
    assert "Nothing can be sent, approved or swapped" not in why


def test_a_chain_with_no_rail_at_all_still_says_nothing_can_move():
    """The blunt wording is correct where it is true. robinhood is money_enabled
    but unfunded; use an unknown-rail row instead — any non-money EVM row."""
    from core.wallet import chains
    row = chains.ChainRow(name="norail", native_symbol="X",
                          purpose="A chain with no rail.", money_enabled=False)
    real = dict(chains._ROWS)
    chains._ROWS["norail"] = row
    try:
        ok, why = chains.money_capable("norail")
    finally:
        chains._ROWS.clear()
        chains._ROWS.update(real)
    assert ok is False
    assert "Nothing can be sent, approved or swapped" in why
