"""Token identity: address is the key, and self-reported metadata is untrusted.

symbol()/name()/decimals() are chosen by the token contract. Reading them over
RPC does not make them true — a hostile token can return different values per
call, omit them entirely (all three are optional in ERC-20), or change them via
a proxy upgrade. `decimals` denominates every valuation, so it is pinned for
canonical tokens and frozen on first sight for everything else.
"""
import pytest

from core.wallet import tokens as T

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # canonical Base USDC
WETH = "0x4200000000000000000000000000000000000006"
FAKE = "0x1111111111111111111111111111111111111111"

_DECIMALS_SEL = "0x313ce567"
_SYMBOL_SEL = "0x95d89b41"


def _word(n: int) -> str:
    return "0x" + f"{n:064x}"


def _abi_string(s: str) -> str:
    """ABI-encoded dynamic string: offset, length, padded data."""
    raw = s.encode()
    return ("0x" + f"{32:064x}" + f"{len(raw):064x}"
            + raw.hex().ljust(64, "0"))


# --------------------------------------------------------------------------
# Address validation
# --------------------------------------------------------------------------

def test_bad_checksum_is_refused_not_normalized():
    bad = "0x833589fcD6eDb6E08f4c7C32D4f71b54bdA02913"  # one char case-flipped
    with pytest.raises(ValueError, match="checksum"):
        T.normalize_address(bad)


def test_all_lowercase_is_accepted_and_checksummed():
    assert T.normalize_address(USDC.lower()) == USDC


def test_all_uppercase_body_is_accepted():
    assert T.normalize_address("0x" + USDC[2:].upper()) == USDC


def test_bad_length_refused():
    with pytest.raises(ValueError):
        T.normalize_address("0x1234")


def test_non_hex_refused():
    with pytest.raises(ValueError):
        T.normalize_address("not-an-address")


# --------------------------------------------------------------------------
# Canonical pinning
# --------------------------------------------------------------------------

def test_canonical_token_is_verified_and_never_reads_the_chain(tmp_path):
    def boom(method, params, chain):
        raise AssertionError("must not hit RPC for a canonical token")

    ident = T.get_token_identity("base", USDC, db_path=str(tmp_path / "t.db"), rpc=boom)
    assert ident.verified is True
    assert ident.decimals == 6
    assert ident.symbol == "USDC"
    assert ident.source == "canonical"
    assert ident.metadata_changed is False


def test_canonical_lookup_is_checksum_insensitive(tmp_path):
    ident = T.get_token_identity("base", WETH.lower(), db_path=str(tmp_path / "t.db"),
                                 rpc=lambda *a, **k: None)
    assert ident.verified is True
    assert ident.decimals == 18


# --------------------------------------------------------------------------
# Frozen first-seen metadata
# --------------------------------------------------------------------------

def test_unknown_token_freezes_first_seen_decimals(tmp_path):
    db = str(tmp_path / "t.db")
    state = {"decimals": 18}

    def rpc(method, params, chain):
        data = params[0]["data"]
        if data.startswith(_DECIMALS_SEL):
            return _word(state["decimals"])
        if data.startswith(_SYMBOL_SEL):
            return _abi_string("EVIL")
        return _abi_string("Evil Token")

    first = T.get_token_identity("base", FAKE, db_path=db, rpc=rpc)
    assert first.decimals == 18
    assert first.verified is False
    assert first.metadata_changed is False
    assert first.source == "first_seen"

    # The contract now reports something different — a proxy upgrade, or simply
    # a token that lies on the second call.
    state["decimals"] = 6
    second = T.get_token_identity("base", FAKE, db_path=db, rpc=rpc)
    assert second.decimals == 18, "the frozen first-seen value must win"
    assert second.metadata_changed is True
    assert second.source == "frozen"


def test_stable_metadata_does_not_flag_changed(tmp_path):
    db = str(tmp_path / "t.db")

    def rpc(method, params, chain):
        data = params[0]["data"]
        if data.startswith(_DECIMALS_SEL):
            return _word(9)
        if data.startswith(_SYMBOL_SEL):
            return _abi_string("CALM")
        return _abi_string("Calm Token")

    T.get_token_identity("base", FAKE, db_path=db, rpc=rpc)
    again = T.get_token_identity("base", FAKE, db_path=db, rpc=rpc)
    assert again.metadata_changed is False
    assert again.decimals == 9


def test_missing_decimals_is_unknown_not_zero(tmp_path):
    def rpc(method, params, chain):
        raise T.onchain.RpcError("execution reverted")

    ident = T.get_token_identity("base", FAKE, db_path=str(tmp_path / "t.db"), rpc=rpc)
    assert ident.decimals is None, "a token without decimals() is unknown, never 0"
    assert ident.verified is False


def test_symbol_is_never_a_lookup_key(tmp_path):
    """There must be no symbol->address path anywhere in this module."""
    assert not hasattr(T, "get_token_by_symbol")
    assert not hasattr(T, "resolve_symbol")


def test_bad_address_refused_before_any_rpc(tmp_path):
    def boom(method, params, chain):
        raise AssertionError("must validate before touching the chain")

    with pytest.raises(ValueError):
        T.get_token_identity("base", "0xdeadbeef", db_path=str(tmp_path / "t.db"), rpc=boom)


# --- per-chain canonical pins (2026-08-17) ----------------------------------
# The pins were Base-only, so Ethereum's USDC — the most-traded ERC-20 there —
# had no pinned identity and fell back to reading the chain.

def test_ethereum_usdc_and_weth_are_pinned_with_their_own_addresses():
    from core.wallet.tokens import canonical_token
    usdc = canonical_token("ethereum", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
    assert usdc and usdc["symbol"] == "USDC" and usdc["decimals"] == 6
    weth = canonical_token("ethereum", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    assert weth and weth["symbol"] == "WETH" and weth["decimals"] == 18


def test_base_pins_survive_the_multi_chain_rewrite():
    from core.wallet.tokens import canonical_token
    assert canonical_token("base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    assert canonical_token("base", "0x4200000000000000000000000000000000000006")


def test_a_chains_usdc_is_never_canonical_on_another_chain():
    """Identity is (chain, address). Base's USDC address on Ethereum is a
    different contract — pinning across chains would assert a symbol for a
    contract nobody verified."""
    from core.wallet.tokens import canonical_token
    assert canonical_token("ethereum",
                           "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913") is None
    assert canonical_token("base",
                           "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48") is None


def test_unverified_chains_carry_no_pins():
    """A chain whose ADDRESSES were never checked must not claim the
    'canonical/verified' status that only an operator check confers. Arbitrum
    and Polygon held this role until 029 §4 verified and armed them; Robinhood
    still does.

    The gate is `money_enabled or assets_verified`, not `money_enabled` alone.
    Solana is verified WITHOUT being money_enabled: its value moves through
    `solana_swap`, never the EVM rail, but its USDC mint is the same constant
    solana_x402.py pins the live settlement rail against. Gating on
    `money_enabled` alone left that mint decimals-unknown, and `portfolio`
    filed the treasury's own USDC under 'NOT necessarily holdings you bought'
    (live prod, 2026-08-28)."""
    from core.wallet.tokens import canonical_token
    from core.wallet import chains
    checked = 0
    for row in chains.all_rows():
        if row.money_enabled or row.assets_verified or not row.usdc:
            continue
        checked += 1
        assert canonical_token(row.name, row.usdc) is None, row.name


def test_the_pin_gate_skips_an_unverified_row():
    """Asserted against a synthetic row, not the registry: every row shipped
    today is verified, so the loop above is vacuous right now and would keep
    passing if the gate were deleted. This is the test that actually holds the
    gate shut for the next chain someone adds."""
    from core.wallet import chains
    from core.wallet.tokens import _canonical_pins
    unverified = chains.ChainRow(
        name="testchain", native_symbol="TST",
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        wrapped_native="0x4200000000000000000000000000000000000006",
        money_enabled=False, assets_verified=False)
    real = dict(chains._ROWS)
    chains._ROWS["testchain"] = unverified
    try:
        pins = _canonical_pins()
    finally:
        chains._ROWS.clear()
        chains._ROWS.update(real)
    assert not any(c == "testchain" for c, _ in pins)


def test_assets_verified_never_implies_money_enabled():
    """The two flags are independent on purpose. `assets_verified` says the
    token addresses were checked; it must never be read as 'value may move
    through the EVM rail here' — Solana is verified and NOT money_enabled."""
    from core.wallet import chains
    row = chains.get("solana")
    assert row.assets_verified is True
    assert row.money_enabled is False
    assert "solana" not in chains.money_chains()
    assert "solana" not in chains.swap_chains()


def test_a_verified_chain_does_carry_its_pins():
    """Whatever quote asset it has. Robinhood has no routable stablecoin, so
    its pin is the wrapped native instead of USDC."""
    from core.wallet.tokens import canonical_token
    from core.wallet import chains
    for row in chains.evm_rows():
        if not row.money_enabled:
            continue
        pinned = row.usdc or row.wrapped_native
        assert canonical_token(row.name, pinned), row.name
