"""Uniswap v3 arithmetic, pure and integer — TickMath, LiquidityAmounts, fee growth.

Nothing here touches a network. Every constant below is the reference
implementation's own (TickMath.sol, LiquidityAmounts.sol, Position.sol) and the
tests pin the periphery's published vectors, the same way `abi.py` pins two real
transactions byte-for-byte: a formula that is subtly wrong produces amounts a pool
ACCEPTS and books as a different deposit.
"""
from __future__ import annotations

import math
from typing import Tuple

Q96 = 2 ** 96
Q128 = 2 ** 128
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
_U256 = 2 ** 256

_MAGIC = [
    (0x1, 0xfffcb933bd6fad37aa2d162d1a594001),
    (0x2, 0xfff97272373d413259a46990580e213a),
    (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
    (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0),
    (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053),
    (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54),
    (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9),
    (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5),
    (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6),
    (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604),
    (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2),
]


def sqrt_price_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick — exact port."""
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError(f"tick {tick} outside [{MIN_TICK}, {MAX_TICK}]")
    abs_tick = -tick if tick < 0 else tick
    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if abs_tick & 0x1 else 0x100000000000000000000000000000000
    for bit, magic in _MAGIC[1:]:
        if abs_tick & bit:
            ratio = (ratio * magic) >> 128
    if tick > 0:
        ratio = (_U256 - 1) // ratio
    # Q128.128 -> Q64.96, rounding up
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def tick_at_sqrt_price(sqrt_x96: int) -> int:
    """Largest tick whose sqrt price <= sqrt_x96 (binary search over the exact
    forward function — slower than the assembly log but provably consistent)."""
    if sqrt_x96 < MIN_SQRT_RATIO or sqrt_x96 >= MAX_SQRT_RATIO:
        raise ValueError("sqrt price outside the tick range")
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sqrt_price_at_tick(mid) <= sqrt_x96:
            lo = mid
        else:
            hi = mid - 1
    return lo


def align_tick(tick: int, spacing: int) -> int:
    """Round `tick` toward zero to the nearest multiple of `spacing` — matches
    Solidity's truncating integer division (`tick / spacing * spacing`), which
    for a negative tick is NOT the same as Python's floor `//` (verified against
    align_tick(-887272, 60) == -887220, not -887280)."""
    q = abs(tick) // spacing
    if tick < 0:
        q = -q
    return q * spacing


def full_range_ticks(spacing: int) -> Tuple[int, int]:
    hi = (MAX_TICK // spacing) * spacing
    return (-hi, hi)


def sqrt_price_from_price(price0_in_1: float, dec0: int, dec1: int) -> int:
    """price0_in_1 = how many token1 (human units) one token0 (human units) buys."""
    raw = price0_in_1 * (10 ** dec1) / (10 ** dec0)
    return int(math.sqrt(raw) * Q96)


def price_to_tick(price0_in_1: float, dec0: int, dec1: int) -> int:
    return tick_at_sqrt_price(sqrt_price_from_price(price0_in_1, dec0, dec1))


def tick_to_price(tick: int, dec0: int, dec1: int) -> float:
    s = sqrt_price_at_tick(tick) / Q96
    return (s * s) * (10 ** dec0) / (10 ** dec1)


def _liq0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    intermediate = (sqrt_a * sqrt_b) // Q96
    return (amount0 * intermediate) // (sqrt_b - sqrt_a)


def _liq1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return (amount1 * Q96) // (sqrt_b - sqrt_a)


def liquidity_for_amounts(sqrt_p: int, sqrt_a: int, sqrt_b: int,
                           amount0: int, amount1: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_p <= sqrt_a:
        return _liq0(sqrt_a, sqrt_b, amount0)
    if sqrt_p < sqrt_b:
        return min(_liq0(sqrt_p, sqrt_b, amount0), _liq1(sqrt_a, sqrt_p, amount1))
    return _liq1(sqrt_a, sqrt_b, amount1)


def _amt0(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return ((liquidity << 96) * (sqrt_b - sqrt_a) // sqrt_b) // sqrt_a


def _amt1(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return (liquidity * (sqrt_b - sqrt_a)) // Q96


def amounts_for_liquidity(sqrt_p: int, sqrt_a: int, sqrt_b: int,
                           liquidity: int) -> Tuple[int, int]:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_p <= sqrt_a:
        return (_amt0(sqrt_a, sqrt_b, liquidity), 0)
    if sqrt_p < sqrt_b:
        return (_amt0(sqrt_p, sqrt_b, liquidity), _amt1(sqrt_a, sqrt_p, liquidity))
    return (0, _amt1(sqrt_a, sqrt_b, liquidity))


def uncollected_fees(*, liquidity: int, fg0_now: int, fg1_now: int,
                      fg0_last: int, fg1_last: int, owed0: int, owed1: int) -> Tuple[int, int]:
    """Position.update: tokensOwed + liquidity * (feeGrowthInside_now - _last) / Q128,
    with the subtraction wrapping mod 2^256 as the contract's does."""
    d0 = (fg0_now - fg0_last) % _U256
    d1 = (fg1_now - fg1_last) % _U256
    return (owed0 + (liquidity * d0) // Q128, owed1 + (liquidity * d1) // Q128)


def sort_tokens(a: str, b: str) -> Tuple[str, str, bool]:
    """(token0, token1, flipped) — token0 is the numerically lower address."""
    a_l, b_l = a.lower(), b.lower()
    if int(a_l, 16) < int(b_l, 16):
        return (a_l, b_l, False)
    return (b_l, a_l, True)
