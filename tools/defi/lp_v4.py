"""Uniswap v4 liquidity on a Pons-graduated PoolKey (048 phase 3; core handoff W6).

Reads (StateView ``getSlot0``/``getLiquidity``, with the PoolManager
``extsload`` fallback), the Permit2 allowance read, and the PositionManager
``modifyLiquidities`` encoder for ONE shape: mint a new position, settle the
pair, sweep the unused native back. Nothing here signs or broadcasts; the
guard (``core.wallet.tx_guard``) is the one authorizer.

The PoolKey is never taken from the caller. It is derived from the Pons
factory's own ``launched_token`` record (``lp_reads.pons_pool_key``): native
``address(0)`` when the record's ``pairToken`` is the native pair. Measured on
2026-09-29: the PNL record gives ``keccak(abi.encode(key)) ==
0x43b7…1e94``, the pool the collection's mint contract buys from —
GeckoTerminal's "PNL/WETH" label is a display name, not the PoolKey.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from core.wallet import abi, dex_registry, univ3_math as M
from tools.defi.lp_reads import LpReadError, _POOL_KEY_FIELDS, pons_pool_key, pool_id, view

ZERO = "0x" + "0" * 40
MAX_UINT128 = 2 ** 128 - 1
MAX_UINT160 = 2 ** 160 - 1

#: v4-periphery ``Actions`` ids (Actions.sol).
ACTION_MINT_POSITION = 0x02
ACTION_SETTLE_PAIR = 0x0D
ACTION_SWEEP = 0x14

#: ``ModifyLiquidity(PoolId indexed id, address indexed sender, int24 tickLower,
#: int24 tickUpper, int256 liquidityDelta, bytes32 salt)`` — emitted by the
#: PoolManager; v4 PositionManager emits no Increase/Decrease events.
TOPIC_MODIFY_LIQUIDITY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"

#: StateLibrary: ``_pools`` mapping slot, and the liquidity offset in Pool.State.
_POOLS_SLOT = 6
_LIQUIDITY_OFFSET = 3

STATE_VIEW_GET_SLOT0 = {"name": "getSlot0", "inputs": [{"type": "bytes32"}],
                        "outputs": [{"type": "uint160"}, {"type": "int24"},
                                    {"type": "uint24"}, {"type": "uint24"}]}
STATE_VIEW_GET_LIQUIDITY = {"name": "getLiquidity", "inputs": [{"type": "bytes32"}],
                            "outputs": [{"type": "uint128"}]}
POOL_MANAGER_EXTSLOAD = {"name": "extsload", "inputs": [{"type": "bytes32"}],
                         "outputs": [{"type": "bytes32"}]}
PERMIT2_ALLOWANCE = {"name": "allowance",
                     "inputs": [{"type": "address"}, {"type": "address"}, {"type": "address"}],
                     "outputs": [{"type": "uint160"}, {"type": "uint48"}, {"type": "uint48"}]}
PERMIT2_APPROVE = {"name": "approve",
                   "inputs": [{"type": "address"}, {"type": "address"},
                              {"type": "uint160"}, {"type": "uint48"}],
                   "outputs": []}
POSM_MODIFY_LIQUIDITIES = {"name": "modifyLiquidities",
                           "inputs": [{"type": "bytes"}, {"type": "uint256"}],
                           "outputs": []}
POSM_NEXT_TOKEN_ID = {"name": "nextTokenId", "inputs": [], "outputs": [{"type": "uint256"}]}


def topic_modify_liquidity() -> str:
    from eth_utils import keccak
    return "0x" + keccak(
        text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)").hex()


# ==========================================================================
# PoolKey
# ==========================================================================

def pons_key_for(rpc, token: str) -> Dict[str, Any]:
    """The v4 PoolKey a Pons token graduated into, from the factory record.
    Refuses a non-Pons token or one still on its curve."""
    from tools.launchpad import pons
    try:
        pons.verify_pins(rpc)
        record = pons.launched_token(rpc, token)
    except pons.PonsError as exc:
        raise LpReadError(f"the Pons factory record for {token} is unreadable: {exc}") from exc
    if not record:
        raise LpReadError(f"{token} is not a Pons-launched token; v4 liquidity is "
                          f"built only for a Pons graduated PoolKey")
    if int(record.get("phase") or 0) < 2:
        raise LpReadError(f"{token} has not graduated off its Pons curve; there is "
                          f"no v4 pool to add to")
    return pons_pool_key(record)


# ==========================================================================
# Pool state — StateView when pinned, else PoolManager.extsload
# ==========================================================================

@dataclass(frozen=True)
class V4PoolState:
    pool_id: str
    sqrt_price_x96: int
    tick: int
    protocol_fee: int
    lp_fee: int
    liquidity: int
    source: str             # "state_view" | "extsload"


def _state_slot(pid: str) -> int:
    from eth_utils import keccak
    return int.from_bytes(keccak(bytes.fromhex(pid[2:]) + _POOLS_SLOT.to_bytes(32, "big")), "big")


def decode_slot0_word(word: int) -> Tuple[int, int, int, int]:
    """StateLibrary.getSlot0 on the packed word: price (160) | tick (24, signed)
    | protocolFee (24) | lpFee (24)."""
    sqrt_price = word & MAX_UINT160
    tick = (word >> 160) & 0xFFFFFF
    if tick >= 1 << 23:
        tick -= 1 << 24
    return sqrt_price, tick, (word >> 184) & 0xFFFFFF, (word >> 208) & 0xFFFFFF


def _word(value) -> int:
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    return int(str(value), 16)


def pool_state(rpc, chain: str, pid: str) -> V4PoolState:
    """Slot0 and in-range liquidity of a v4 pool. An uninitialized pool (price
    0) is returned as such — the caller decides; an unreadable one raises."""
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.pool_manager:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v4 deployment")
    if row.state_view:
        sp, tick, pfee, lfee = view(rpc, row.state_view, STATE_VIEW_GET_SLOT0, [pid])
        liq = view(rpc, row.state_view, STATE_VIEW_GET_LIQUIDITY, [pid])
        return V4PoolState(pid, int(sp), int(tick), int(pfee), int(lfee), int(liq),
                           "state_view")
    slot = _state_slot(pid)
    word = _word(view(rpc, row.pool_manager, POOL_MANAGER_EXTSLOAD,
                      ["0x" + slot.to_bytes(32, "big").hex()]))
    liq_word = _word(view(rpc, row.pool_manager, POOL_MANAGER_EXTSLOAD,
                          ["0x" + (slot + _LIQUIDITY_OFFSET).to_bytes(32, "big").hex()]))
    sp, tick, pfee, lfee = decode_slot0_word(word)
    return V4PoolState(pid, sp, tick, pfee, lfee, liq_word & MAX_UINT128, "extsload")


def permit2_allowance(rpc, chain: str, owner: str, token: str, spender: str) -> Tuple[int, int]:
    """``(amount, expiration)`` of the Permit2 grant owner→spender on token."""
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.permit2:
        raise LpReadError(f"chain {chain!r} pins no Permit2")
    amount, expiration, _nonce = view(rpc, row.permit2, PERMIT2_ALLOWANCE, [owner, token, spender])
    return int(amount), int(expiration)


# ==========================================================================
# Encoders
# ==========================================================================

def _key_tuple(key: Dict[str, Any]):
    return (key["currency0"], key["currency1"], int(key["fee"]),
            int(key["tickSpacing"]), key["hooks"])


def encode_mint_unlock(key, tick_lower, tick_upper, liquidity, amount0_max,
                       amount1_max, owner) -> bytes:
    """``abi.encode(bytes actions, bytes[] params)`` for MINT_POSITION,
    SETTLE_PAIR, SWEEP(currency0 → owner). The SWEEP returns the native the
    mint did not use; with an ERC-20 currency0 it sweeps nothing."""
    mint = abi.encode(
        _POOL_KEY_FIELDS + [{"type": "int24"}, {"type": "int24"}, {"type": "uint256"},
                            {"type": "uint128"}, {"type": "uint128"}, {"type": "address"},
                            {"type": "bytes"}],
        [_key_tuple(key), int(tick_lower), int(tick_upper), int(liquidity),
         int(amount0_max), int(amount1_max), owner, b""])
    settle = abi.encode([{"type": "address"}, {"type": "address"}],
                        [key["currency0"], key["currency1"]])
    sweep = abi.encode([{"type": "address"}, {"type": "address"}], [key["currency0"], owner])
    actions = bytes([ACTION_MINT_POSITION, ACTION_SETTLE_PAIR, ACTION_SWEEP])
    return abi.encode([{"type": "bytes"}, {"type": "bytes[]"}],
                      [actions, [mint, settle, sweep]])


def encode_modify_liquidities(unlock: bytes, deadline: int) -> str:
    return abi.encode_call(POSM_MODIFY_LIQUIDITIES["name"], POSM_MODIFY_LIQUIDITIES["inputs"],
                           [unlock, int(deadline)])


def encode_permit2_approve(token: str, spender: str, amount: int, expiration: int) -> str:
    return abi.encode_call(PERMIT2_APPROVE["name"], PERMIT2_APPROVE["inputs"],
                           [token, spender, int(amount), int(expiration)])


def full_range_quote(sqrt_price_x96: int, tick_spacing: int, amount0: int,
                     amount1: int) -> Tuple[int, int, int, int, int]:
    """``(tick_lower, tick_upper, liquidity, used0, used1)`` for a full-range
    deposit bounded by both amounts (either may be ``MAX_UINT128`` = unbounded)."""
    lo, hi = M.full_range_ticks(int(tick_spacing))
    sa, sb = M.sqrt_price_at_tick(lo), M.sqrt_price_at_tick(hi)
    liq = M.liquidity_for_amounts(int(sqrt_price_x96), sa, sb, int(amount0), int(amount1))
    used0, used1 = M.amounts_for_liquidity(int(sqrt_price_x96), sa, sb, liq)
    return lo, hi, liq, used0, used1


def reserves_full_range(sqrt_price_x96: int, liquidity: int) -> Tuple[float, float]:
    """Virtual reserves ``(x, y)`` in RAW units of the in-range liquidity at the
    current price: ``x = L / sqrtP``, ``y = L · sqrtP``. For a pool whose
    liquidity is one full-range position this is its real depth; with
    concentrated ranges it is the LOCAL depth — the figure a small swap sees."""
    s = sqrt_price_x96 / M.Q96
    if s <= 0:
        return 0.0, 0.0
    return liquidity / s, liquidity * s


# ==========================================================================
# Read renders for defi_data.lp_quote / lp_pool_info (protocol='v4')
# ==========================================================================

def _is_native(text: Optional[str]) -> bool:
    return str(text or "").strip().lower() in ("native", ZERO)


def resolve_pons_pair(rpc, token_a: Optional[str], token_b: Optional[str]):
    """``(key, pool_id, flipped)`` for a Pons pair named as token_a/token_b.
    ``flipped`` is True when token_a is currency1. Refuses a pair that is not
    exactly the record's two currencies."""
    a, b = str(token_a or ""), str(token_b or "")
    token = b if _is_native(a) else a
    if not token or _is_native(token):
        raise LpReadError("name the Pons token as token_a or token_b (the other "
                          "side is 'native' for a native-pair launch)")
    key = pons_key_for(rpc, token)
    want = {key["currency0"].lower(), key["currency1"].lower()}
    got = {ZERO if _is_native(x) else x.lower() for x in (a, b)}
    if got != want:
        raise LpReadError(f"the pair {a}/{b} is not this token's Pons PoolKey "
                          f"({key['currency0']}/{key['currency1']})")
    flipped = (ZERO if _is_native(a) else a.lower()) == key["currency1"].lower()
    return key, pool_id(key), flipped


def pool_info_lines(rpc, chain: str, token_a, token_b) -> list:
    key, pid, _ = resolve_pons_pair(rpc, token_a, token_b)
    st = pool_state(rpc, chain, pid)
    if st.sqrt_price_x96 == 0:
        return [f"v4 pool {pid} on {chain} is NOT initialized — nothing to read"]
    s = st.sqrt_price_x96 / M.Q96
    p = s * s  # currency1 raw per currency0 raw
    x, y = reserves_full_range(st.sqrt_price_x96, st.liquidity)
    return [
        f"v4 pool {pid} on {chain} — {key['currency0']}/{key['currency1']} "
        f"lpFee {st.lp_fee} hooks {key['hooks']} tickSpacing {key['tickSpacing']}",
        f"  tick: {st.tick}   sqrtPriceX96: {st.sqrt_price_x96}",
        f"  liquidity (in range): {st.liquidity:,}",
        f"  price: 1 currency0 = {p:.10g} currency1 (raw ratio; decimals not applied)",
        f"  in-range virtual reserves (raw): currency0 {int(x):,}  currency1 {int(y):,}",
        f"  read via: {st.source}",
    ]


def quote_lines(rpc, chain: str, token_a, token_b, amount_a, amount_b,
                range_text: str, decimals_fn: Callable) -> list:
    if range_text != "full":
        raise LpReadError("v4 quotes are full range only (090 R2.1); pass range='full'")
    key, pid, flipped = resolve_pons_pair(rpc, token_a, token_b)
    st = pool_state(rpc, chain, pid)
    if st.sqrt_price_x96 == 0:
        raise LpReadError(f"v4 pool {pid} is not initialized; nothing to quote")
    from core.wallet.tokens import raw_amount, bounded_decimals
    dec0 = 18 if key["currency0"] == ZERO else bounded_decimals(decimals_fn(key["currency0"]))
    dec1 = bounded_decimals(decimals_fn(key["currency1"]))
    if dec0 is None or dec1 is None:
        raise LpReadError("the pool has unsupported token decimals")
    h0, h1 = (amount_b, amount_a) if flipped else (amount_a, amount_b)
    if h0 is None and h1 is None:
        raise LpReadError("give amount_a and/or amount_b to quote a deposit")
    try:
        raw0 = MAX_UINT128 if h0 is None else raw_amount(h0, dec0)
        raw1 = MAX_UINT128 if h1 is None else raw_amount(h1, dec1)
    except ValueError as exc:
        raise LpReadError(str(exc)) from exc
    lo, hi, liq, u0, u1 = full_range_quote(st.sqrt_price_x96, key["tickSpacing"], raw0, raw1)
    x, _ = reserves_full_range(st.sqrt_price_x96, st.liquidity)
    after = x + u0
    return [
        f"lp_quote {chain} v4 pool {pid} (Pons PoolKey, hooks {key['hooks']})",
        f"  ticks: [{lo},{hi}] (full range)",
        f"  liquidity: {liq:,}",
        f"  amount0: {u0 / 10 ** dec0:.10g} (currency0 {key['currency0']})",
        f"  amount1: {u1 / 10 ** dec1:.10g} (currency1 {key['currency1']})",
        f"  currency0 in-range depth: {x / 10 ** dec0:.6g} -> {after / 10 ** dec0:.6g}",
        "  LP fee 0: this pool pays its fee to the hook, not to the position",
    ]
