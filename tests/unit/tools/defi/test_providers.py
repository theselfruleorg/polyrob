"""Provider parsers — pure, fixture-driven, no network.

Fixtures are real API responses (DexScreener search/token, GoPlus token_security
on Base) with only oversized holder arrays trimmed. `goplus_honeypot.json` is the
same real schema with its security fields flipped to a known-bad shape.
"""
import json
import pathlib

import pytest

from tools.defi.providers import dexscreener, goplus

FX = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    return json.loads((FX / name).read_text())


# --------------------------------------------------------------------------
# DexScreener — ambiguity preservation and price confidence
# --------------------------------------------------------------------------

def test_search_preserves_same_chain_ambiguity():
    """Two Base contracts both claiming AERO must BOTH survive."""
    cands = dexscreener.parse_search(_fx("dexscreener_typosquat.json"))
    assert len(cands) == 2, "a symbol search must not collapse to one token"
    assert len({c.address for c in cands}) == 2
    assert {c.symbol for c in cands} == {"AERO"}, "identical ticker, different contracts"


def test_ranking_does_not_protect_against_a_seeded_pool():
    """Liquidity ranking is a display convenience, not a safety signal — the
    typosquat with the deeper (purchasable) pool ranks FIRST, which is precisely
    why resolution must never auto-pick."""
    cands = dexscreener.parse_search(_fx("dexscreener_typosquat.json"))
    assert cands[0].address == "0xBAdBadbADBaDBADBadbADbaDBadBaDBADBadBAD0"


def test_search_is_ranked_by_liquidity_descending():
    cands = dexscreener.parse_search(_fx("dexscreener_search.json"))
    liq = [c.liquidity_usd for c in cands]
    assert liq == sorted(liq, reverse=True)


def test_unsupported_chains_are_dropped_deliberately():
    """The real fixture carries Solana pairs; a Base-only tier drops them by an
    explicit chain filter, not by accident of address validation."""
    all_pairs = _fx("dexscreener_search.json")["pairs"]
    assert any(p["chainId"] != "base" for p in all_pairs), "fixture must contain other chains"
    cands = dexscreener.parse_search(_fx("dexscreener_search.json"))
    assert cands and all(c.chain == "base" for c in cands)


def test_search_addresses_are_checksummed():
    for c in dexscreener.parse_search(_fx("dexscreener_search.json")):
        assert c.address.startswith("0x") and len(c.address) == 42
        assert c.address != c.address.lower(), "must be EIP-55 checksummed"


def test_search_carries_chain_so_identity_is_chain_scoped():
    cands = dexscreener.parse_search(_fx("dexscreener_search.json"))
    assert all(c.chain for c in cands)
    assert len({c.chain for c in cands}) >= 1


AERO = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_parse_pair_gives_price_and_liquidity():
    info = dexscreener.parse_pair(_fx("dexscreener_pair.json"), AERO)
    assert info.price_usd is not None and info.price_usd > 0
    assert info.liquidity_usd is not None
    assert info.pool_count >= 1


def test_deep_liquidity_multi_pool_is_high_confidence():
    info = dexscreener.parse_pair(_fx("dexscreener_pair.json"), AERO)
    assert info.confidence == "high"


def test_quote_side_pools_never_supply_the_price():
    """DexScreener's priceUsd is always the BASE token's price.

    Caught live: /tokens/<USDC> returns 30 pools, 6 of them with USDC as the
    QUOTE side. Taking the deepest pool regardless of side priced USDC at $0.44
    — the AERO price. In a valuation path that understates a USDC holding by
    more than half.
    """
    payload = {"pairs": [
        {   # deepest, but our token is the QUOTE side
            "chainId": "base",
            "baseToken": {"address": AERO, "symbol": "AERO"},
            "quoteToken": {"address": USDC, "symbol": "USDC"},
            "priceUsd": "0.4423",
            "liquidity": {"usd": 90_000_000.0},
        },
        {   # shallower, but our token IS the base
            "chainId": "base",
            "baseToken": {"address": USDC, "symbol": "USDC"},
            "quoteToken": {"address": AERO, "symbol": "AERO"},
            "priceUsd": "0.9999",
            "liquidity": {"usd": 1_000_000.0},
        },
    ]}
    info = dexscreener.parse_pair(payload, USDC)
    assert info.price_usd == pytest.approx(0.9999), "must price the token we asked about"
    assert info.pool_count == 1, "only base-side pools price the token"


def test_token_present_only_as_quote_has_no_price():
    payload = {"pairs": [{
        "chainId": "base",
        "baseToken": {"address": AERO, "symbol": "AERO"},
        "quoteToken": {"address": USDC, "symbol": "USDC"},
        "priceUsd": "0.4423",
        "liquidity": {"usd": 90_000_000.0},
    }]}
    info = dexscreener.parse_pair(payload, USDC)
    assert info.price_usd is None, "unknown beats another token's price"
    assert info.confidence == "unknown"


def test_thin_single_pool_is_low_confidence():
    payload = {"pairs": [{
        "chainId": "base",
        "baseToken": {"address": "0x" + "11" * 20, "symbol": "SCAM", "name": "Scam"},
        "priceUsd": "1000.0",
        "liquidity": {"usd": 900.0},
    }]}
    info = dexscreener.parse_pair(payload, "0x1111111111111111111111111111111111111111")
    assert info.confidence == "low", "an attacker-seedable pool is not high confidence"


def test_absent_price_is_unknown_confidence_not_zero():
    info = dexscreener.parse_pair({"pairs": []}, AERO)
    assert info.price_usd is None
    assert info.confidence == "unknown"


def test_none_payload_is_unknown():
    info = dexscreener.parse_pair(None, AERO)
    assert info.price_usd is None
    assert info.confidence == "unknown"


# --------------------------------------------------------------------------
# GoPlus — enumerated checks, never a boolean "safe"
# --------------------------------------------------------------------------

def test_screen_enumerates_checks():
    v = goplus.parse_screen(_fx("goplus_clean.json"))
    assert v.available is True
    assert isinstance(v.checks, dict) and v.checks, "the verdict must enumerate checks"
    assert "is_honeypot" in v.checks


def test_verdict_has_no_boolean_safe_field():
    v = goplus.parse_screen(_fx("goplus_clean.json"))
    assert not hasattr(v, "safe"), "'passed' would read as 'safe'; enumerate instead"
    assert not hasattr(v, "is_safe")


def test_established_token_carries_no_critical_flags():
    """Real AERO on Base screens clean of the dangerous patterns.

    It IS `is_mintable`, which the parser correctly surfaces — most real tokens
    are. That is why the verdict enumerates rather than returning a verdict
    word: "has a flag" and "is a rug" are different statements.
    """
    v = goplus.parse_screen(_fx("goplus_clean.json"))
    critical = {"honeypot", "cannot_sell_all", "cannot_buy", "blacklist", "closed_source"}
    assert not (critical & set(v.flags))
    assert "mintable" in v.flags, "a real, surfaced, non-critical flag"


def test_honeypot_is_flagged():
    v = goplus.parse_screen(_fx("goplus_honeypot.json"))
    assert "honeypot" in v.flags
    assert "cannot_sell_all" in v.flags


def test_high_sell_tax_is_flagged():
    v = goplus.parse_screen(_fx("goplus_honeypot.json"))
    assert any("sell_tax" in f for f in v.flags)


def test_unavailable_screen_is_not_a_pass():
    v = goplus.parse_screen(None)
    assert v.available is False
    assert v.flags == []
    assert v.checks == {}


def test_api_error_payload_is_unavailable():
    v = goplus.parse_screen({"code": 0, "message": "rate limited", "result": {}})
    assert v.available is False


def test_empty_result_for_unknown_token_is_unavailable():
    v = goplus.parse_screen({"code": 1, "message": "OK", "result": {}})
    assert v.available is False, "no data is not a clean bill of health"


# --- multi-chain provider scoping (2026-08-17) -------------------------------
# Each provider knows a DIFFERENT set of chains, and the honest answer for a
# chain a provider does not index is "unavailable", never another chain's data.

def test_dexscreener_chain_scope_comes_from_the_registry():
    from core.wallet import chains
    from tools.defi.providers import dexscreener
    expected = {r.dexscreener_id for r in chains.all_rows() if r.dexscreener_id}
    assert set(dexscreener.SUPPORTED_CHAINS) == expected
    assert "ethereum" in dexscreener.SUPPORTED_CHAINS
    assert "base" in dexscreener.SUPPORTED_CHAINS
    # Robinhood is not indexed there — claiming it would fabricate a price.
    assert "robinhood" not in dexscreener.SUPPORTED_CHAINS


def test_the_price_filter_uses_the_providers_own_chain_id(monkeypatch):
    """`/tokens/<addr>` returns pools on EVERY chain (Ethereum USDC comes back
    with pulsechain pairs), so the filter is what stops another chain's pool
    pricing this one's token. It must filter by the row's dexscreener_id, not by
    our chain NAME — if the two ever differ, filtering by the name silently
    matches nothing."""
    from core.wallet import chains
    from tools.defi.providers import dexscreener

    row = chains.get("base")
    monkeypatch.setitem(chains._ROWS, "base",
                        type(row)(**{**row.__dict__, "dexscreener_id": "base-v2"}))
    payload = {"pairs": [
        {"chainId": "base-v2", "baseToken": {"address": "0xaa"},
         "priceUsd": "1.0", "liquidity": {"usd": 100000.0}},
        {"chainId": "otherchain", "baseToken": {"address": "0xaa"},
         "priceUsd": "999.0", "liquidity": {"usd": 900000.0}},
    ]}
    monkeypatch.setattr(dexscreener, "_get", lambda url, timeout: payload)
    info = dexscreener.token("base", "0xaa")
    assert info.price_usd == 1.0, "the deeper foreign-chain pool must not price it"


def test_goplus_chain_ids_come_from_the_registry():
    from core.wallet import chains
    from tools.defi.providers import goplus
    for row in chains.all_rows():
        if row.goplus_id:
            assert goplus.CHAIN_IDS.get(row.name) == row.goplus_id, row.name
    assert "robinhood" not in goplus.CHAIN_IDS


def test_alchemy_url_is_built_from_the_chains_slug():
    from tools.defi.providers import alchemy_index
    assert alchemy_index.base_url_for("base").startswith(
        "https://base-mainnet.g.alchemy.com/")
    assert alchemy_index.base_url_for("ethereum").startswith(
        "https://eth-mainnet.g.alchemy.com/")


def test_alchemy_refuses_a_chain_it_has_no_slug_for():
    """No slug means no indexer for that chain; guessing a URL would query the
    wrong network."""
    from tools.defi.providers import alchemy_index
    assert alchemy_index.base_url_for("nosuchchain") is None
    assert alchemy_index.fetch_balances("0x" + "11" * 20, chain="nosuchchain") is None
