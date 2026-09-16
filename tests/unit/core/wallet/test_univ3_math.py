"""Pure Uniswap v3 math, pinned to the reference implementation's own vectors."""
import math
import pytest
from core.wallet import univ3_math as M


def test_tickmath_reference_vectors():
    assert M.sqrt_price_at_tick(0) == 2 ** 96
    assert M.sqrt_price_at_tick(M.MIN_TICK) == 4295128739
    assert M.sqrt_price_at_tick(M.MAX_TICK) == 1461446703485210103287273052203988822378723970342


def test_tick_at_sqrt_price_round_trips():
    for t in (-887272, -100000, -1, 0, 1, 12345, 887271):
        assert M.tick_at_sqrt_price(M.sqrt_price_at_tick(t)) == t


def test_tick_outside_range_refuses():
    with pytest.raises(ValueError):
        M.sqrt_price_at_tick(887273)


def test_align_and_full_range():
    assert M.align_tick(-887272, 60) == -887220
    assert M.align_tick(123457, 200) == 123400
    assert M.full_range_ticks(200) == (-887200, 887200)
    assert M.full_range_ticks(10) == (-887270, 887270)


def _enc(reserve1, reserve0):
    # encodePriceSqrt from the v3-periphery test utils: sqrt(reserve1/reserve0) * 2^96
    return int(math.isqrt((reserve1 << 192) // reserve0))


def test_liquidity_amounts_periphery_vector_price_inside():
    # LiquidityAmounts.spec.ts "amounts for price inside": 100/200 -> 2148, back -> 99/99
    p, a, b = _enc(1, 1), _enc(100, 110), _enc(110, 100)
    L = M.liquidity_for_amounts(p, a, b, 100, 200)
    assert L == 2148
    assert M.amounts_for_liquidity(p, a, b, L) == (99, 99)


def test_liquidity_amounts_price_below_and_above():
    a, b = _enc(100, 110), _enc(110, 100)
    below = _enc(99, 110)
    assert M.liquidity_for_amounts(below, a, b, 100, 200) == 1048
    assert M.amounts_for_liquidity(below, a, b, 1048) == (99, 0)
    above = _enc(111, 100)
    assert M.liquidity_for_amounts(above, a, b, 100, 200) == 2097
    assert M.amounts_for_liquidity(above, a, b, 2097) == (0, 199)


def test_price_tick_conversion_with_decimals():
    # 1 WETH (18) = 2500 USDC (6): token0=USDC, token1=WETH -> price1per0 = 1/2500 adjusted
    tick = M.price_to_tick(2500.0, 6, 18)   # price of token0 in token1 units
    back = M.tick_to_price(tick, 6, 18)
    assert abs(back - 2500.0) / 2500.0 < 0.001


def test_uncollected_fees_wraps_q128():
    Q128 = 2 ** 128
    fees0, fees1 = M.uncollected_fees(
        liquidity=10 ** 18, fg0_now=2 * Q128, fg1_now=5,
        fg0_last=1 * Q128, fg1_last=(2 ** 256) - 5, owed0=7, owed1=0)
    assert fees0 == 10 ** 18 + 7            # one full Q128 of growth = liquidity tokens
    assert fees1 == (10 * 10 ** 18) // Q128  # wrapped delta of 10, floors to 0


def test_sort_tokens():
    a, b = "0x00000000000000000000000000000000000000AA", "0x0000000000000000000000000000000000000001"
    assert M.sort_tokens(a, b) == (b.lower(), a.lower(), True)
