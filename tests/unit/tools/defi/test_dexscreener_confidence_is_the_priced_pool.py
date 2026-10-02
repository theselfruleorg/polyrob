"""`confidence` must describe the depth behind the QUOTED price, not a sum.

⚠️ The defect (2026-09-23, found while tracing the PNL two-pool confusion):
``parse_pair`` takes ``price_usd`` from the **deepest single pool** but
``liquidity_usd`` as the **sum of every base-side pool**, and then graded
``confidence`` from that sum. Those describe different things, and the grade was
being made against the wrong one.

Why it matters: ``confidence`` is not decoration. ``data_tool`` EXCLUDES a
holding's value from a total unless the price is "high"
(``data_tool.py:1368``, ``:1750``), and ``trade_tool._priced`` returns None
unless it is "high" (``trade_tool.py:662``). And ``PriceInfo``'s own docstring
says "low" means *"the price is attacker-influenceable and must not be summed
into a headline total"* — which is a statement about the pool the price came
from. Liquidity sitting in pools we are NOT quoting does not make the quoted
number any harder to move.

So the grade is now made on the priced pool's own depth, and that figure is
carried on ``priced_liquidity_usd`` so a caller can see the basis instead of
inferring it. ``liquidity_usd`` still reports the total, because it is a real
figure callers already depend on.

**This change can only ever DEMOTE** — the priced pool's liquidity is by
construction ≤ the total — so it can exclude a value from a headline or withhold
a price from the trade path, never admit one that was previously refused.
"""
import pytest

from tools.defi.providers.base import LIQUIDITY_CONFIDENCE_FLOOR_USD as FLOOR
from tools.defi.providers.dexscreener import parse_pair

TOKEN = "0x" + "a" * 40


def _payload(*pools, base=TOKEN):
    """``/tokens/<addr>`` body: each pool is (liquidity_usd, priceUsd)."""
    return {"pairs": [
        {"baseToken": {"address": base, "symbol": "TKN", "name": "Token"},
         "liquidity": {"usd": liq}, "priceUsd": price}
        for liq, price in pools
    ]}


# --- the defect itself --------------------------------------------------------- #

def test_a_thin_priced_pool_is_not_high_just_because_other_pools_are_deep():
    """Ten equal pools summing far above the floor. The quoted price still comes
    from ONE of them and costs that one pool's depth to move."""
    info = parse_pair(_payload(*[(FLOOR / 5, "1.0")] * 10), TOKEN)
    assert info.liquidity_usd == pytest.approx(FLOOR * 2)   # the sum is unchanged
    assert info.priced_liquidity_usd == pytest.approx(FLOOR / 5)
    assert info.confidence == "low"


def test_a_priced_pool_that_clears_the_floor_is_still_high():
    info = parse_pair(_payload((FLOOR * 2, "1.0"), (10.0, "9.0")), TOKEN)
    assert info.price_usd == pytest.approx(1.0)             # deepest pool's price
    assert info.priced_liquidity_usd == pytest.approx(FLOOR * 2)
    assert info.confidence == "high"


def test_the_basis_is_carried_so_a_caller_never_has_to_infer_it():
    info = parse_pair(_payload((900.0, "2.0"), (100.0, "7.0")), TOKEN)
    assert (info.liquidity_usd, info.priced_liquidity_usd) == (1000.0, 900.0)


# --- what must NOT change ------------------------------------------------------ #

def test_the_total_is_still_the_total():
    """Callers depend on `liquidity_usd` meaning all base-side pools."""
    info = parse_pair(_payload((10.0, "1.0"), (20.0, "1.0"), (30.0, "1.0")), TOKEN)
    assert info.liquidity_usd == pytest.approx(60.0)
    assert info.pool_count == 3


def test_the_price_is_still_the_deepest_pools_price():
    info = parse_pair(_payload((5.0, "3.0"), (50.0, "4.0"), (1.0, "9.0")), TOKEN)
    assert info.price_usd == pytest.approx(4.0)


def test_a_token_that_is_only_ever_a_quote_still_has_no_price():
    """The pre-existing guard: another token's number is never borrowed."""
    other = {"pairs": [{"baseToken": {"address": "0x" + "b" * 40},
                        "liquidity": {"usd": 10_000_000.0}, "priceUsd": "0.44"}]}
    info = parse_pair(other, TOKEN)
    assert (info.price_usd, info.liquidity_usd, info.pool_count) == (None, None, 0)
    assert info.confidence == "unknown"
    assert info.priced_liquidity_usd is None


def test_a_zero_price_is_still_unknown_however_deep_the_pool():
    info = parse_pair(_payload((FLOOR * 10, "0")), TOKEN)
    assert info.confidence == "unknown"


def test_a_single_deep_pool_is_still_low_because_one_pool_is_one_venue():
    """`pool_count >= 2` was already required and is untouched."""
    info = parse_pair(_payload((FLOOR * 5, "1.0")), TOKEN)
    assert info.confidence == "low"


# --- the direction guarantee --------------------------------------------------- #

def test_the_change_can_only_demote_never_promote():
    """priced ≤ total by construction, so no input that graded 'low' before can
    grade 'high' now. Swept across a spread of shapes."""
    for n in (2, 3, 5, 10):
        for each in (1.0, FLOOR / 10, FLOOR / 2, FLOOR, FLOOR * 3):
            info = parse_pair(_payload(*[(each, "1.0")] * n), TOKEN)
            assert info.priced_liquidity_usd <= info.liquidity_usd
            if info.confidence == "high":
                # would also have been "high" under the old total-based rule
                assert info.liquidity_usd >= FLOOR and info.pool_count >= 2
