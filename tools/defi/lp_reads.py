"""Uniswap v3 chain reads for the liquidity rail — pool discovery, pool state,
position view, owned-position enumeration, and the Pons v4-graduation PoolKey.

Everything here is a READ. No calldata is built, nothing is signed. The one
rule that matters: an unreadable figure raises `LpReadError`, it is never
reported as 0/[]/None — except `pool_address`, where the factory returning the
zero address IS the honest answer "no pool exists for this pair/fee", not a
read failure.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.wallet import abi, dex_registry, univ3_math
from tools.defi import lp_abi as A
from tools.launchpad import pons_abi as P

_ZERO_ADDRESS = "0x" + "0" * 40
_U256 = 2 ** 256
_MAX_OWNED_POSITIONS = 200


class LpReadError(RuntimeError):
    """A chain read for the liquidity rail returned nothing usable. Never guess
    a value the contract did not give — an unreadable figure is an error, not
    a zero."""


# ==========================================================================
# The one view primitive — same contract as tools/launchpad/pons.py::_view.
# ==========================================================================

def view(rpc, to: str, spec: Dict[str, Any], args: Optional[List] = None):
    """Encode-call-decode a single view function. Raises `LpReadError` when the
    node returns nothing (None/""/"0x") — that is a failed read, not a zero."""
    data = abi.encode_call(spec["name"], spec["inputs"], args or [])
    try:
        raw = rpc("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as exc:
        raise LpReadError(f"{spec['name']} on {to} unavailable: {exc}") from exc
    if raw in (None, "0x", ""):
        raise LpReadError(
            f"{spec['name']} on {to} returned nothing — refusing to guess a "
            f"value the contract did not give")
    try:
        values = abi.decode(spec["outputs"], raw)
    except Exception as exc:
        raise LpReadError(f"{spec['name']} on {to} unreadable: {exc}") from exc
    return values[0] if len(values) == 1 else values


# ==========================================================================
# Pool discovery
# ==========================================================================

def pool_address(rpc, chain: str, token_a: str, token_b: str,
                  fee: int) -> Optional[str]:
    """The v3 pool for (token_a, token_b, fee), or None when the factory says
    none exists (the zero address IS that answer, not a read failure)."""
    row = dex_registry.row_for(chain, "v3")
    if row is None or not row.factory:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v3 factory")
    pool = view(rpc, row.factory, A.FACTORY_GET_POOL, [token_a, token_b, int(fee)])
    if str(pool).lower() == _ZERO_ADDRESS:
        return None
    return str(pool)


# ==========================================================================
# Pool state
# ==========================================================================

@dataclass(frozen=True)
class PoolState:
    pool: str
    token0: str
    token1: str
    fee: int
    tick_spacing: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    dec0: int
    dec1: int
    price0_in_1: float


def pool_state(rpc, chain: str, pool: str) -> PoolState:
    """Everything needed to price and range a v3 pool, read live."""
    token0 = str(view(rpc, pool, A.POOL_TOKEN0))
    token1 = str(view(rpc, pool, A.POOL_TOKEN1))
    fee = int(view(rpc, pool, A.POOL_FEE))
    tick_spacing = int(view(rpc, pool, A.POOL_TICK_SPACING))
    slot0 = view(rpc, pool, A.POOL_SLOT0)
    sqrt_price_x96, tick = int(slot0[0]), int(slot0[1])
    liquidity = int(view(rpc, pool, A.POOL_LIQUIDITY))
    dec0 = int(view(rpc, token0, A.ERC20_DECIMALS))
    dec1 = int(view(rpc, token1, A.ERC20_DECIMALS))
    price0_in_1 = (sqrt_price_x96 / univ3_math.Q96) ** 2 * (10 ** dec0) / (10 ** dec1)
    return PoolState(
        pool=pool, token0=token0, token1=token1, fee=fee,
        tick_spacing=tick_spacing, sqrt_price_x96=sqrt_price_x96, tick=tick,
        liquidity=liquidity, dec0=dec0, dec1=dec1, price0_in_1=price0_in_1)


# ==========================================================================
# Fee growth — Tick.getFeeGrowthInside, exact port
# ==========================================================================

def fee_growth_inside(fg_global: Tuple[int, int], lower_tick_row: Sequence,
                       upper_tick_row: Sequence, tick_lower: int, tick_upper: int,
                       tick_current: int) -> Tuple[int, int]:
    """Tick.getFeeGrowthInside, ported exactly — every subtraction wraps mod
    2**256 the same way the contract's unchecked block does."""
    fg0_global, fg1_global = fg_global
    lower_out0, lower_out1 = int(lower_tick_row[2]), int(lower_tick_row[3])
    upper_out0, upper_out1 = int(upper_tick_row[2]), int(upper_tick_row[3])

    if tick_current >= tick_lower:
        below0, below1 = lower_out0, lower_out1
    else:
        below0 = (fg0_global - lower_out0) % _U256
        below1 = (fg1_global - lower_out1) % _U256

    if tick_current < tick_upper:
        above0, above1 = upper_out0, upper_out1
    else:
        above0 = (fg0_global - upper_out0) % _U256
        above1 = (fg1_global - upper_out1) % _U256

    inside0 = (fg0_global - below0 - above0) % _U256
    inside1 = (fg1_global - below1 - above1) % _U256
    return inside0, inside1


# ==========================================================================
# Position view
# ==========================================================================

@dataclass(frozen=True)
class PositionView:
    token_id: int
    pool: str
    token0: str
    token1: str
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    amount0: int
    amount1: int
    fees0: int
    fees1: int
    in_range: bool
    dec0: int
    dec1: int


def position(rpc, chain: str, token_id: int) -> PositionView:
    """A v3 NPM position: amounts at the current price plus uncollected fees."""
    row = dex_registry.row_for(chain, "v3")
    if row is None or not row.position_manager:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v3 position manager")

    pos = view(rpc, row.position_manager, A.NPM_POSITIONS, [int(token_id)])
    (_nonce, _operator, token0, token1, fee, tick_lower, tick_upper, liquidity,
     fg0_last, fg1_last, owed0, owed1) = pos
    fee, tick_lower, tick_upper = int(fee), int(tick_lower), int(tick_upper)
    liquidity = int(liquidity)

    pool = pool_address(rpc, chain, str(token0), str(token1), fee)
    if pool is None:
        raise LpReadError(
            f"position {token_id}: no pool for {token0}/{token1} fee {fee} on "
            f"{chain!r} — the position cannot be read")

    ps = pool_state(rpc, chain, pool)

    fg0_global = int(view(rpc, pool, A.POOL_FEE_GROWTH_GLOBAL0))
    fg1_global = int(view(rpc, pool, A.POOL_FEE_GROWTH_GLOBAL1))
    lower_row = view(rpc, pool, A.POOL_TICKS, [tick_lower])
    upper_row = view(rpc, pool, A.POOL_TICKS, [tick_upper])

    fg0_now, fg1_now = fee_growth_inside(
        (fg0_global, fg1_global), lower_row, upper_row,
        tick_lower, tick_upper, ps.tick)

    fees0, fees1 = univ3_math.uncollected_fees(
        liquidity=liquidity, fg0_now=fg0_now, fg1_now=fg1_now,
        fg0_last=int(fg0_last), fg1_last=int(fg1_last),
        owed0=int(owed0), owed1=int(owed1))

    sqrt_a = univ3_math.sqrt_price_at_tick(tick_lower)
    sqrt_b = univ3_math.sqrt_price_at_tick(tick_upper)
    amount0, amount1 = univ3_math.amounts_for_liquidity(
        ps.sqrt_price_x96, sqrt_a, sqrt_b, liquidity)

    in_range = tick_lower <= ps.tick < tick_upper

    return PositionView(
        token_id=int(token_id), pool=pool, token0=str(token0), token1=str(token1),
        fee=fee, tick_lower=tick_lower, tick_upper=tick_upper, liquidity=liquidity,
        amount0=amount0, amount1=amount1, fees0=fees0, fees1=fees1,
        in_range=in_range, dec0=ps.dec0, dec1=ps.dec1)


# ==========================================================================
# Owned positions
# ==========================================================================

def owned_position_ids(rpc, chain: str, owner: str) -> List[int]:
    """Every v3 NPM token id *owner* holds, capped at 200 (an unbounded
    enumeration is an unbounded number of RPC round-trips per call)."""
    row = dex_registry.row_for(chain, "v3")
    if row is None or not row.position_manager:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v3 position manager")
    npm = row.position_manager
    balance = int(view(rpc, npm, A.NPM_BALANCE_OF, [owner]))
    if balance > _MAX_OWNED_POSITIONS:
        raise LpReadError(f"{balance} positions exceed enumeration limit {_MAX_OWNED_POSITIONS}; list unavailable, not empty")
    count = balance
    ids: List[int] = []
    for index in range(count):
        token_id = view(rpc, npm, A.NPM_TOKEN_OF_OWNER_BY_INDEX, [owner, index])
        ids.append(int(token_id))
    return ids


# ==========================================================================
# ERC-20 helpers
# ==========================================================================

def allowance(rpc, chain: str, token: str, owner: str, spender: str) -> int:
    return int(view(rpc, token, A.ERC20_ALLOWANCE, [owner, spender]))


def decimals(rpc, chain: str, token: str) -> int:
    return int(view(rpc, token, A.ERC20_DECIMALS))


# ==========================================================================
# Pons -> Uniswap v4 PoolKey, for a token that graduated off the curve.
# ==========================================================================

_POOL_KEY_FIELDS = [{"type": "tuple", "components": [
    {"name": "currency0", "type": "address"},
    {"name": "currency1", "type": "address"},
    {"name": "fee", "type": "uint24"},
    {"name": "tickSpacing", "type": "int24"},
    {"name": "hooks", "type": "address"},
]}]


def pons_pool_key(record: Dict[str, Any], native_pair: str = P.NATIVE_PAIR) -> Dict[str, Any]:
    """The v4 PoolKey a Pons `launched_token` record graduates into.

    `record["pairToken"]` is a Pons pair-token address; when it names the
    chain's native pair (the default, `pons_abi.NATIVE_PAIR`) the corresponding
    v4 currency is the native placeholder `address(0)`, never the pair token
    itself — v4 represents native currency as the zero address, not a wrapper.
    """
    token = record["token"]
    pair_token = record["pairToken"]
    currency_pair = _ZERO_ADDRESS if str(pair_token).lower() == str(native_pair).lower() else pair_token
    currency0, currency1, _flipped = univ3_math.sort_tokens(token, currency_pair)
    return {
        "currency0": currency0,
        "currency1": currency1,
        "fee": int(record["poolFee"]),
        "tickSpacing": int(record["tickSpacing"]),
        "hooks": P.MEME_HOOK,
    }


def pool_id(key: Dict[str, Any]) -> str:
    """`keccak(abi.encode(PoolKey))` — the v4 PoolId, matching PoolIdLibrary."""
    from eth_utils import keccak
    values = [(key["currency0"], key["currency1"], key["fee"],
               key["tickSpacing"], key["hooks"])]
    encoded = abi.encode(_POOL_KEY_FIELDS, values)
    return "0x" + keccak(encoded).hex()


def collectible_fees(rpc, chain: str, token_id: int, owner: str) -> Tuple[int, int]:
    """Actual collectible amounts via eth_call. Pool rounding can leave the
    nominal fee-growth estimate a few raw units above what collect delivers.
    This read executes no transaction and requires no signer.
    """
    npm = dex_registry.resolve_position_manager(chain, "v3")
    data = abi.encode_call(A.NPM_COLLECT["name"], A.NPM_COLLECT["inputs"],
                           [(token_id, owner, A.MAX_UINT128, A.MAX_UINT128)])
    try:
        raw = rpc("eth_call", [{"from": owner, "to": npm, "data": data}, "latest"])
        return tuple(int(n) for n in abi.decode(A.NPM_COLLECT["outputs"], raw))
    except Exception as exc:
        raise LpReadError(f"collectible fees unavailable: {exc}") from exc
