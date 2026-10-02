"""A price must say WHICH pool it came from.

⚠️ The defect, proven in production on 2026-09-23: the SAFETY rail reported PNL
liquidity as **$4,133** at 04:21Z and **$17,887** at 08:21Z with no flag and no
explanation. Neither pool had moved — measured directly, the traded ``1%`` pool
held $4,103 (−86.3%, $54k daily volume) and the reserve-top pool held $17,860
(+2.6%, $1.5k volume) — so the rail had silently changed which venue it read,
and **nothing in its output could have told anyone that.**

`token_info` is the read those rails consume, and its line was:

    price: $0.000032   confidence: low   pools: 2   liquidity: $18,091

Three numbers from three different scopes: the price is ONE pool's, the
liquidity is the SUM of all of them, and the pool count is the only hint that
those are not the same thing. So `PriceInfo` now carries the priced pool's
ADDRESS, and the render names it. A reader comparing two reports can then see a
venue change instead of inferring a market crash.

None of this guesses: an address the provider did not give stays None and the
render says so rather than inventing attribution.
"""
import pytest

from tools.defi.providers.dexscreener import parse_pair

TOKEN = "0x" + "a" * 40
POOL_A = "0x" + "1" * 40
POOL_B = "0x" + "2" * 40


def _payload(*pools, base=TOKEN):
    """``/tokens/<addr>``: each pool is (pairAddress|None, liquidity, priceUsd)."""
    out = []
    for addr, liq, price in pools:
        pair = {"baseToken": {"address": base, "symbol": "TKN", "name": "Token"},
                "liquidity": {"usd": liq}, "priceUsd": price}
        if addr is not None:
            pair["pairAddress"] = addr
        out.append(pair)
    return {"pairs": out}


# --- the defect itself --------------------------------------------------------- #

def test_the_price_names_the_pool_it_came_from():
    info = parse_pair(_payload((POOL_A, 900.0, "2.0"), (POOL_B, 100.0, "7.0")), TOKEN)
    assert info.priced_pool_address == POOL_A      # the deepest pair, whose price we took
    assert info.price_usd == pytest.approx(2.0)


def test_two_reports_of_the_same_token_can_be_told_apart_by_venue():
    """The production failure in miniature: same token, different venue, and the
    only honest way to notice is that the address changed."""
    first = parse_pair(_payload((POOL_A, 4103.0, "0.0000039")), TOKEN)
    later = parse_pair(_payload((POOL_B, 17860.0, "0.0000323")), TOKEN)
    assert first.priced_pool_address != later.priced_pool_address
    # …and each still reports its own depth, so neither looks like the other.
    assert (first.priced_liquidity_usd, later.priced_liquidity_usd) == (4103.0, 17860.0)


# --- never invent attribution -------------------------------------------------- #

def test_a_provider_that_gives_no_address_yields_None_not_a_guess():
    info = parse_pair(_payload((None, 900.0, "2.0")), TOKEN)
    assert info.priced_pool_address is None
    assert info.price_usd == pytest.approx(2.0)   # the price is still usable


def test_a_token_with_no_base_side_pool_has_no_pool_to_name():
    other = {"pairs": [{"baseToken": {"address": "0x" + "b" * 40},
                        "liquidity": {"usd": 10.0}, "priceUsd": "1.0",
                        "pairAddress": POOL_A}]}
    info = parse_pair(other, TOKEN)
    assert (info.price_usd, info.priced_pool_address) == (None, None)


def test_a_blank_address_is_treated_as_absent():
    info = parse_pair(_payload(("   ", 900.0, "2.0")), TOKEN)
    assert info.priced_pool_address is None


# --- what must not change ------------------------------------------------------ #

def test_the_totals_and_the_grade_are_untouched():
    info = parse_pair(_payload((POOL_A, 900.0, "2.0"), (POOL_B, 100.0, "7.0")), TOKEN)
    assert info.liquidity_usd == pytest.approx(1000.0)   # still the SUM
    assert info.pool_count == 2
    assert info.confidence == "low"                      # graded on the priced pool


def test_the_field_defaults_so_older_constructions_still_build():
    from tools.defi.providers.base import PriceInfo
    p = PriceInfo(price_usd=1.0, liquidity_usd=2.0, pool_count=1, confidence="low")
    assert p.priced_pool_address is None
    assert p.priced_liquidity_usd is None


# --- the render a person actually reads ---------------------------------------- #

def test_the_rendered_line_names_the_venue_and_its_own_depth():
    from tools.defi.data_tool import _price_line
    info = parse_pair(_payload((POOL_A, 4103.0, "0.0000039"), (POOL_B, 17860.0, "0.0000323")), TOKEN)
    line = _price_line(info)
    assert POOL_B in line                    # the deepest pair supplied the price
    assert "priced pool" in line
    assert "total" in line                   # the sum is still labelled as a total


def test_the_rendered_line_says_so_when_the_venue_is_unknown():
    from tools.defi.data_tool import _price_line
    info = parse_pair(_payload((None, 900.0, "2.0")), TOKEN)
    line = _price_line(info)
    assert "pool not named by the provider" in line
