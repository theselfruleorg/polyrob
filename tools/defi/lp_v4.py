"""Uniswap v4 liquidity on a Pons-graduated PoolKey (048 phase 3; core handoff W6).

Reads (StateView ``getSlot0``/``getLiquidity``, with the PoolManager
``extsload`` fallback), the Permit2 allowance read, and the PositionManager
``modifyLiquidities`` encoder for TWO shapes: mint a new position, settle the
pair, sweep the unused native back; and collect a position's fees (a
decrease of 0, take the pair to the holder; 115 W4). Nothing here signs or
broadcasts; the guard (``core.wallet.tx_guard``) is the one authorizer.

The PoolKey is never taken from the caller. It is derived from the Pons
factory's own ``launched_token`` record (``lp_reads.pons_pool_key``): native
``address(0)`` when the record's ``pairToken`` is the native pair. Measured on
2026-09-29: one launched token's record gives ``keccak(abi.encode(key)) ==
0x43b7…1e94``, the pool the collection's mint contract buys from —
GeckoTerminal's "TOKEN/WETH" label is a display name, not the PoolKey.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from core.wallet import abi, dex_registry, univ3_math as M
from tools.defi.lp_reads import LpReadError, _POOL_KEY_FIELDS, pons_pool_key, pool_id, view

ZERO = "0x" + "0" * 40
MAX_UINT128 = 2 ** 128 - 1
MAX_UINT160 = 2 ** 160 - 1

#: v4-periphery ``Actions`` ids (Actions.sol).
ACTION_DECREASE_LIQUIDITY = 0x01
ACTION_MINT_POSITION = 0x02
ACTION_SETTLE_PAIR = 0x0D
ACTION_TAKE_PAIR = 0x11
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
POSM_POSITION_LIQUIDITY = {"name": "getPositionLiquidity", "inputs": [{"type": "uint256"}],
                           "outputs": [{"type": "uint128"}]}
POSM_OWNER_OF = {"name": "ownerOf", "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]}


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


def encode_collect_unlock(token_id, currency0, currency1, recipient) -> bytes:
    """``abi.encode(bytes actions, bytes[] params)`` for DECREASE_LIQUIDITY of
    0 (``tokenId, 0, 0, 0, ""``: it removes no liquidity, it only settles the
    fees owed) and TAKE_PAIR(currency0, currency1 → recipient). Nothing is
    settled in, so no approval and no Permit2 are involved (115 W4)."""
    decrease = abi.encode([{"type": "uint256"}, {"type": "uint256"}, {"type": "uint128"},
                           {"type": "uint128"}, {"type": "bytes"}],
                          [int(token_id), 0, 0, 0, b""])
    take = abi.encode([{"type": "address"}, {"type": "address"}, {"type": "address"}],
                      [currency0, currency1, recipient])
    actions = bytes([ACTION_DECREASE_LIQUIDITY, ACTION_TAKE_PAIR])
    return abi.encode([{"type": "bytes"}, {"type": "bytes[]"}], [actions, [decrease, take]])


def position_pool_key(rpc, chain: str, token_id: int) -> Dict[str, Any]:
    """The PoolKey of a v4 position, read from the pinned PositionManager
    (``getPoolAndPositionInfo``) — never from the caller. The guard reads it
    again for itself (``liquidity_guard.read_v4_position_key``)."""
    from core.wallet.liquidity_guard import read_v4_position_key
    try:
        return read_v4_position_key(rpc, chain, token_id)
    except ValueError as exc:
        raise LpReadError(str(exc)) from exc


def encode_decrease_unlock(token_id, liquidity, min0, min1, currency0, currency1, recipient) -> bytes:
    """117 W7: DECREASE_LIQUIDITY(tokenId, liquidity, min0, min1, "") +
    TAKE_PAIR(currency0, currency1 → recipient): a withdrawal that keeps the
    position NFT. The two minima are the on-chain floor."""
    decrease = abi.encode([{"type": "uint256"}, {"type": "uint256"}, {"type": "uint128"},
                           {"type": "uint128"}, {"type": "bytes"}],
                          [int(token_id), int(liquidity), int(min0), int(min1), b""])
    take = abi.encode([{"type": "address"}, {"type": "address"}, {"type": "address"}],
                      [currency0, currency1, recipient])
    actions = bytes([ACTION_DECREASE_LIQUIDITY, ACTION_TAKE_PAIR])
    return abi.encode([{"type": "bytes"}, {"type": "bytes[]"}], [actions, [decrease, take]])


@dataclass(frozen=True)
class V4Position:
    token_id: int
    owner: str
    key: Dict[str, Any]
    pool_id: str
    tick_lower: int
    tick_upper: int
    liquidity: int


def decode_position_info(info: int) -> Tuple[int, int]:
    """``(tickLower, tickUpper)`` from a v4 ``PositionInfo`` word: poolId (200
    bits) | tickUpper (24) | tickLower (24) | hasSubscriber (8)."""
    def i24(x):
        x &= 0xFFFFFF
        return x - (1 << 24) if x >= 1 << 23 else x
    return i24(int(info) >> 8), i24(int(info) >> 32)


def position(rpc, chain: str, token_id: int) -> V4Position:
    """One v4 position as the PINNED PositionManager reports it: owner, PoolKey,
    ticks, liquidity. A position that does not exist, or a read that fails,
    raises — it is never an empty position."""
    from core.wallet.liquidity_guard import POSM_POOL_AND_POSITION_INFO
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.position_manager:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v4 deployment")
    npm = row.position_manager
    try:
        owner = str(view(rpc, npm, POSM_OWNER_OF, [int(token_id)])).lower()
    except LpReadError as exc:
        if "NOT_MINTED" in str(exc):
            raise LpReadError(f"v4 position #{token_id} does not exist on {chain} (never "
                              f"minted, or burned)") from exc
        raise
    (c0, c1, fee, spacing, hooks), info = view(rpc, npm, POSM_POOL_AND_POSITION_INFO, [int(token_id)])
    key = {"currency0": str(c0).lower(), "currency1": str(c1).lower(), "fee": int(fee),
           "tickSpacing": int(spacing), "hooks": str(hooks).lower()}
    if not int(spacing):
        raise LpReadError(f"v4 position #{token_id} has no PoolKey on {npm}")
    lo, hi = decode_position_info(info)
    liq = int(view(rpc, npm, POSM_POSITION_LIQUIDITY, [int(token_id)]))
    return V4Position(int(token_id), owner, key, pool_id(key), lo, hi, liq)


STATE_VIEW_GET_POSITION_INFO = {
    "name": "getPositionInfo",
    "inputs": [{"type": "bytes32"}, {"type": "address"}, {"type": "int24"}, {"type": "int24"},
               {"type": "bytes32"}],
    "outputs": [{"type": "uint128"}, {"type": "uint256"}, {"type": "uint256"}]}
STATE_VIEW_GET_FEE_GROWTH_INSIDE = {
    "name": "getFeeGrowthInside",
    "inputs": [{"type": "bytes32"}, {"type": "int24"}, {"type": "int24"}],
    "outputs": [{"type": "uint256"}, {"type": "uint256"}]}
#: Swap(bytes32 indexed id, address indexed sender, int128 amount0, int128 amount1,
#:      uint160 sqrtPriceX96, uint128 liquidity, int24 tick, uint24 fee)
TOPIC_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"


def position_fees(rpc, chain: str, pos: V4Position) -> Tuple[int, int]:
    """The fees owed to a v4 position now, raw ``(currency0, currency1)``: the
    growth of fees inside its range since its last touch, times its liquidity
    (the PoolManager's own formula, read through the pinned StateView). The
    position's owner in the PoolManager is the PositionManager, its salt the
    NFT id. A failed read raises — fees are never "0" for a read that failed."""
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.state_view or not row.position_manager:
        raise LpReadError(f"chain {chain!r} pins no StateView; the fees owed cannot be read")
    pid = bytes.fromhex(pos.pool_id[2:])
    liq, last0, last1 = view(rpc, row.state_view, STATE_VIEW_GET_POSITION_INFO,
                             [pid, row.position_manager, pos.tick_lower, pos.tick_upper,
                              int(pos.token_id).to_bytes(32, "big")])
    now0, now1 = view(rpc, row.state_view, STATE_VIEW_GET_FEE_GROWTH_INSIDE,
                      [pid, pos.tick_lower, pos.tick_upper])
    owed = lambda now, last: ((int(now) - int(last)) % 2 ** 256) * int(liq) >> 128
    return owed(now0, last0), owed(now1, last1)


def _signed(word: int) -> int:
    return word - (1 << 256) if word >= 1 << 255 else word


def pool_activity(rpc, chain: str, pool_id: str, *, hours: float = 24.0,
                  protocol_fee: int = 0) -> Dict[str, Any]:
    """Swaps of one v4 pool in the last *hours*, from the PoolManager's ``Swap``
    logs: count, volume per currency, and what the LPs earned (the fee of each
    swap is on its input; the protocol's part, *protocol_fee* as the pool holds
    it now, is taken off). The block range comes from a measured block time, so
    the window is approximate and the dict says which blocks it read."""
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.pool_manager:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v4 deployment")
    head = int(rpc("eth_blockNumber", []), 16)
    span = min(head, 100_000)
    if span < 1:
        raise LpReadError("the chain has no blocks to measure")
    stamp = lambda n: int(rpc("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
    block_s = (stamp(head) - stamp(head - span)) / span
    if block_s <= 0:
        raise LpReadError("the block time could not be measured")
    start = max(0, head - int(hours * 3600 / block_s))
    logs = rpc("eth_getLogs", [{"address": row.pool_manager, "fromBlock": hex(start),
                                "toBlock": hex(head), "topics": [TOPIC_SWAP, pool_id]}])
    pf = (int(protocol_fee) & 0xFFF, (int(protocol_fee) >> 12) & 0xFFF)   # 0->1, 1->0
    out = {"swaps": 0, "volume0": 0, "volume1": 0, "lp_fees0": 0, "lp_fees1": 0,
           "from_block": start, "to_block": head, "hours": hours}
    for log in logs or []:
        data = bytes.fromhex(str(log.get("data", "0x"))[2:])
        if len(data) < 192:
            raise LpReadError("a Swap log has an unexpected layout")
        word = lambda i: int.from_bytes(data[32 * i:32 * i + 32], "big")
        a0, a1, fee = _signed(word(0)), _signed(word(1)), word(5)
        out["swaps"] += 1
        out["volume0"] += abs(a0)
        out["volume1"] += abs(a1)
        if a0 < 0:                                   # the swapper paid currency0 in
            out["lp_fees0"] += -a0 * max(0, fee - pf[0]) // 10 ** 6
        if a1 < 0:
            out["lp_fees1"] += -a1 * max(0, fee - pf[1]) // 10 ** 6
    return out


def _fee_text(lp_fee: int, protocol_fee: int) -> str:
    p01, p10 = int(protocol_fee) & 0xFFF, (int(protocol_fee) >> 12) & 0xFFF
    text = f"LP fee {int(lp_fee) / 10000:g}%"
    if p01 or p10:
        text += (f" + protocol fee {p01 / 10000:g}%" if p01 == p10 else
                 f" + protocol fee {p01 / 10000:g}% / {p10 / 10000:g}% by direction")
        text += " (the protocol's part does not go to a position)"
    return text


def _raw_text(n: int, dec: Optional[int]) -> str:
    return f"{n} raw" if dec is None else f"{n / 10 ** dec:.10g}"


def activity_lines(rpc, chain: str, pool_id: str, st, dec0: Optional[int],
                   dec1: Optional[int]) -> list:
    """The last-24-hours line pair of a v4 pool, or one NOT READ line."""
    try:
        a = pool_activity(rpc, chain, pool_id, protocol_fee=st.protocol_fee)
    except Exception as exc:
        return [f"  last 24 h: NOT READ ({type(exc).__name__}: {str(exc)[:100]})"]
    return [f"  last 24 h (blocks {a['from_block']}-{a['to_block']}, approximate window): "
            f"{a['swaps']} swap(s), volume {_raw_text(a['volume0'], dec0)} currency0 / "
            f"{_raw_text(a['volume1'], dec1)} currency1",
            f"  LP fees earned in that window: about {_raw_text(a['lp_fees0'], dec0)} currency0 + "
            f"{_raw_text(a['lp_fees1'], dec1)} currency1 (all positions together)"]


def encode_modify_liquidities(unlock: bytes, deadline: int) -> str:
    return abi.encode_call(POSM_MODIFY_LIQUIDITIES["name"], POSM_MODIFY_LIQUIDITIES["inputs"],
                           [unlock, int(deadline)])


POSM_INITIALIZE_POOL = {"name": "initializePool",
                        "inputs": _POOL_KEY_FIELDS + [{"type": "uint160"}],
                        "outputs": [{"type": "int24"}]}


def encode_seed_multicall(key, sqrt_price_x96: int, modify_data: str) -> str:
    """117: ``multicall([initializePool(key, sqrtPriceX96), <modifyLiquidities>])``
    — the pool gets its price and our liquidity in ONE transaction, or neither."""
    init = abi.encode_call(POSM_INITIALIZE_POOL["name"], POSM_INITIALIZE_POOL["inputs"],
                           [_key_tuple(key), int(sqrt_price_x96)])
    as_bytes = lambda h: bytes.fromhex(h[2:])
    return abi.encode_call("multicall", [{"type": "bytes[]"}],
                           [[as_bytes(init), as_bytes(modify_data)]])


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


def explicit_key(token_a, token_b, fee, tick_spacing, hooks=None):
    """117 W5: ``(key, pool_id, flipped)`` for a PoolKey the CALLER names in
    full — a read needs no declaration, a pool id is only a hash of these five
    fields. Currencies are sorted as v4 sorts them (native = address(0) first)."""
    from core.wallet.tokens import normalize_address
    try:
        a = ZERO if _is_native(token_a) else normalize_address(str(token_a)).lower()
        b = ZERO if _is_native(token_b) else normalize_address(str(token_b)).lower()
        hook = ZERO if hooks in (None, "") else normalize_address(str(hooks)).lower()
    except ValueError as exc:
        raise LpReadError(str(exc)) from exc
    if a == b:
        raise LpReadError("a PoolKey needs two different currencies")
    if fee is None or tick_spacing is None or int(tick_spacing) < 1 or int(fee) < 0:
        raise LpReadError("a PoolKey needs fee and tick_spacing (and hooks, when the pool has one)")
    c0, c1 = sorted((a, b), key=lambda x: int(x, 16))
    key = {"currency0": c0, "currency1": c1, "fee": int(fee),
           "tickSpacing": int(tick_spacing), "hooks": hook}
    return key, pool_id(key), a == c1


def resolve_key(rpc, chain, token_a=None, token_b=None, *, fee=None, tick_spacing=None,
                hooks=None, token_id=None):
    """``(key, pool_id, flipped, label)`` for a v4 READ. In order: the key of
    position *token_id*; a key named in full (``tick_spacing`` given); else the
    Pons factory record of the token."""
    if token_id is not None:
        pos = position(rpc, chain, token_id)
        return pos.key, pos.pool_id, False, f"the PoolKey of position #{token_id}"
    if tick_spacing is not None:
        key, pid, flipped = explicit_key(token_a, token_b, fee, tick_spacing, hooks)
        return key, pid, flipped, "the PoolKey as named"
    key, pid, flipped = resolve_pons_pair(rpc, token_a, token_b)
    return key, pid, flipped, "Pons PoolKey"


def _price_text(sqrt_price_x96: int, dec0: Optional[int], dec1: Optional[int]) -> str:
    s = sqrt_price_x96 / M.Q96
    if dec0 is None or dec1 is None:
        return f"1 currency0 = {s * s:.10g} currency1 (raw ratio; decimals not read)"
    p = s * s * 10 ** dec0 / 10 ** dec1
    return f"1 currency0 = {p:.10g} currency1   |   1 currency1 = {1 / p:.10g} currency0"


def _decimals_pair(key, decimals_fn):
    from core.wallet.tokens import bounded_decimals
    try:
        d0 = 18 if key["currency0"] == ZERO else bounded_decimals(decimals_fn(key["currency0"]))
        return d0, bounded_decimals(decimals_fn(key["currency1"]))
    except Exception:
        return None, None


def position_lines(rpc, chain: str, token_id: int, decimals_fn: Callable,
                   holder: Optional[str] = None) -> list:
    """117 W5/W6: one v4 position, read from the chain. ``holder`` (when given)
    is compared with the owner the PositionManager reports."""
    pos = position(rpc, chain, token_id)
    st = pool_state(rpc, chain, pos.pool_id)
    dec0, dec1 = _decimals_pair(pos.key, decimals_fn)
    key = pos.key
    lines = [f"v4 position #{pos.token_id} on {chain} — owner {pos.owner}"
             + ("" if holder is None else
                " (this wallet)" if holder.lower() == pos.owner else " (NOT this wallet)"),
             f"  pool {pos.pool_id} — {key['currency0']}/{key['currency1']} fee {key['fee']} "
             f"tickSpacing {key['tickSpacing']} hooks {key['hooks']}",
             f"  ticks: [{pos.tick_lower},{pos.tick_upper}]   liquidity: {pos.liquidity:,}"]
    if st.sqrt_price_x96 == 0:
        return lines + ["  the pool is NOT initialized — no amounts to read"]
    a0, a1 = M.amounts_for_liquidity(st.sqrt_price_x96, M.sqrt_price_at_tick(pos.tick_lower),
                                     M.sqrt_price_at_tick(pos.tick_upper), pos.liquidity)
    in_range = pos.tick_lower <= st.tick < pos.tick_upper
    amt = _raw_text
    try:
        f0, f1 = position_fees(rpc, chain, pos)
        fees = (f"  fees owed (uncollected): {amt(f0, dec0)} currency0 + {amt(f1, dec1)} "
                f"currency1 — lp_collect(protocol='v4', token_id={pos.token_id}) takes them")
    except Exception as exc:
        fees = f"  fees owed: NOT READ ({type(exc).__name__}: {str(exc)[:100]})"
    return lines + [
        f"  pool tick: {st.tick} ({'in range' if in_range else 'OUT of range'})   "
        f"price: {_price_text(st.sqrt_price_x96, dec0, dec1)}",
        f"  holds now: {amt(a0, dec0)} currency0 + {amt(a1, dec1)} currency1 "
        f"(from the pool price; the fees owed are a separate line)",
        fees,
        f"  {_fee_text(st.lp_fee, st.protocol_fee)}",
        "  source: the pinned PositionManager, PoolManager and StateView, read now",
    ]


def pool_info_lines(rpc, chain: str, token_a=None, token_b=None, *, fee=None, tick_spacing=None,
                    hooks=None, token_id=None, decimals_fn: Optional[Callable] = None) -> list:
    key, pid, _, label = resolve_key(rpc, chain, token_a, token_b, fee=fee,
                                     tick_spacing=tick_spacing, hooks=hooks, token_id=token_id)
    st = pool_state(rpc, chain, pid)
    if st.sqrt_price_x96 == 0:
        return [f"v4 pool {pid} on {chain} ({label}) is NOT initialized — nothing to read"]
    dec0, dec1 = _decimals_pair(key, decimals_fn) if decimals_fn else (None, None)
    x, y = reserves_full_range(st.sqrt_price_x96, st.liquidity)
    lines = [
        f"v4 pool {pid} on {chain} ({label}) — {key['currency0']}/{key['currency1']} "
        f"fee {key['fee']} tickSpacing {key['tickSpacing']} hooks {key['hooks']}",
        f"  tick: {st.tick}   sqrtPriceX96: {st.sqrt_price_x96}   "
        f"{_fee_text(st.lp_fee, st.protocol_fee)}",
        f"  liquidity (in range): {st.liquidity:,}",
        f"  price: {_price_text(st.sqrt_price_x96, dec0, dec1)}",
        f"  in-range virtual reserves: currency0 {_raw_text(int(x), dec0)}  "
        f"currency1 {_raw_text(int(y), dec1)} (local depth, not the pool's whole balance)",
    ]
    if key["hooks"] != ZERO and not st.lp_fee:
        lines.append("  LP fee 0: this pool pays its fee to the hook, not to a position")
    return lines + activity_lines(rpc, chain, pid, st, dec0, dec1) + [f"  read via: {st.source}"]


def quote_lines(rpc, chain: str, token_a, token_b, amount_a, amount_b,
                range_text: str, decimals_fn: Callable, *, fee=None, tick_spacing=None,
                hooks=None, initial_price=None) -> list:
    if tick_spacing is not None:
        return _quote_named_key(rpc, chain, token_a, token_b, amount_a, amount_b, range_text,
                                decimals_fn, fee, tick_spacing, hooks, initial_price)
    if range_text != "full":
        raise LpReadError("v4 quotes on a Pons PoolKey are full range only (090 R2.1); pass "
                          "range='full', or name the PoolKey in full (fee + tick_spacing)")
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


def range_ticks(range_text: str, spacing: int) -> Tuple[int, int]:
    """``'full'`` or ``'ticks:lo,hi'`` (sorted-currency ticks) → aligned ticks."""
    if range_text == "full":
        return M.full_range_ticks(int(spacing))
    if not str(range_text).startswith("ticks:"):
        raise LpReadError("a v4 range is 'full' or 'ticks:lo,hi' in sorted-currency ticks")
    try:
        lo, hi = (int(x) for x in range_text[6:].split(","))
    except ValueError as exc:
        raise LpReadError(f"range {range_text!r} is not 'ticks:lo,hi'") from exc
    if lo % spacing or hi % spacing or not M.MIN_TICK <= lo < hi <= M.MAX_TICK:
        raise LpReadError(f"ticks [{lo},{hi}] must be ordered, in bounds and multiples of {spacing}")
    return lo, hi


def _quote_named_key(rpc, chain, token_a, token_b, amount_a, amount_b, range_text,
                     decimals_fn, fee, tick_spacing, hooks, initial_price) -> list:
    """117 W5: a deposit quote on a PoolKey named in full. An uninitialised pool
    is quoted at ``initial_price`` (token_b per token_a) — the plan of a seed."""
    from core.wallet.tokens import raw_amount
    key, pid, flipped = explicit_key(token_a, token_b, fee, tick_spacing, hooks)
    dec0, dec1 = _decimals_pair(key, decimals_fn)
    if dec0 is None or dec1 is None:
        raise LpReadError("the pool has unsupported or unreadable token decimals")
    st = pool_state(rpc, chain, pid)
    sqrt_p, note = st.sqrt_price_x96, f"pool tick {st.tick}"
    if sqrt_p == 0:
        if initial_price is None or not initial_price > 0:
            raise LpReadError(f"v4 pool {pid} is not initialized; give initial_price "
                              f"(token_b per token_a) to quote its seed")
        p01 = 1 / initial_price if flipped else initial_price
        sqrt_p = M.sqrt_price_from_price(p01, dec0, dec1)
        note = "NOT initialized — quoted at initial_price (a seed)"
    lo, hi = range_ticks(range_text, key["tickSpacing"])
    h0, h1 = (amount_b, amount_a) if flipped else (amount_a, amount_b)
    if h0 is None and h1 is None:
        raise LpReadError("give amount_a and/or amount_b to quote a deposit")
    try:
        raw0 = MAX_UINT128 if h0 is None else raw_amount(h0, dec0)
        raw1 = MAX_UINT128 if h1 is None else raw_amount(h1, dec1)
    except ValueError as exc:
        raise LpReadError(str(exc)) from exc
    sa, sb = M.sqrt_price_at_tick(lo), M.sqrt_price_at_tick(hi)
    liq = M.liquidity_for_amounts(sqrt_p, sa, sb, raw0, raw1)
    u0, u1 = M.amounts_for_liquidity(sqrt_p, sa, sb, liq)
    return [
        f"lp_quote {chain} v4 pool {pid} (the PoolKey as named, hooks {key['hooks']}) — {note}",
        f"  ticks: [{lo},{hi}]   sqrtPriceX96: {sqrt_p}",
        f"  liquidity: {liq:,}",
        f"  amount0: {u0 / 10 ** dec0:.10g} (currency0 {key['currency0']})",
        f"  amount1: {u1 / 10 ** dec1:.10g} (currency1 {key['currency1']})",
        f"  price: {_price_text(sqrt_p, dec0, dec1)}",
    ]

