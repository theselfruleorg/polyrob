"""Liquidity-specific assertions, called only by the shared tx authorizer.

The pinned v3 manager enforces that burn follows a complete decrease/collect.

v4 (048 phase 3, core handoff W6) is judged from the SIGNED calldata, not from
a declaration: a v4 liquidity transaction is ``modifyLiquidities`` on the
pinned PositionManager carrying exactly MINT_POSITION + SETTLE_PAIR + SWEEP,
on a PoolKey whose hook is pinned by code hash, with empty hookData. The one
other v4 shape is the fee COLLECT (115 W4): exactly DECREASE_LIQUIDITY of 0 +
TAKE_PAIR to the holder, on the position's own PoolKey as the PositionManager
reports it — the only liquidity shape a token-bound account may carry
(:func:`may_ride_account`). The WITHDRAWAL (117 W7) is the same two actions with
a positive decrease and a positive on-chain minimum, from the wallet only.
Increase and burn on v4 refuse. Its ERC-20 leg moves
through Permit2, so the one Permit2 grant a verb may declare — an exact,
short-lived grant to the pinned v4 PositionManager — is recognised here too
(``permit2_grant_declared``); every other Permit2 grant still refuses.

The SEED (117) is the mint shape with one more call in front of it:
``multicall([initializePool(key, sqrtPriceX96), modifyLiquidities(mint)])`` on
the same pinned PositionManager, ONE transaction, the two keys equal. An
initialised pool with no liquidity has a free price, so "initialise" and "mint"
are never two transactions here: a bare ``initializePool`` refuses. The price
is bounded by the SIGNED bytes (:func:`_assert_price_bound`): each signed
maximum is at most what the signed liquidity needs at the signed price plus
``SEED_MAX_GAP_BPS``, so the PositionManager itself reverts at any other price.
"""
import time
from math import isfinite

from core.wallet import abi, dex_registry

ZERO = "0x" + "0" * 40

#: The longest a declared Permit2 grant may live. A tranche is one grant, one
#: add, minutes apart (090 R2.2 deadline <= 2 min); an hour bounds a grant the
#: add never consumed.
PERMIT2_GRANT_MAX_TTL_S = 3600

#: ``ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)`` from the
#: v4 PoolManager; v4 PositionManager emits no Increase/Decrease events.
TOPIC_V4_MODIFY_LIQUIDITY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"

#: ``Initialize(bytes32 indexed id, address indexed currency0, address indexed
#: currency1, uint24 fee, int24 tickSpacing, address hooks, uint160
#: sqrtPriceX96, int24 tick)`` from the v4 PoolManager (measured on 4663).
TOPIC_V4_INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
#: The widest gap between a signed maximum and the amount the signed liquidity
#: needs: at the SIGNED price for a seed, at the pool's own price for a mint
#: into an initialised pool (the verb's slippage maximum plus rounding).
SEED_MAX_GAP_BPS = 500
MINT_MAX_GAP_BPS = 1100
V4_DYNAMIC_FEE_FLAG = 0x800000
V4_MAX_LP_FEE = 1_000_000

_V4_MINT_SHAPE = bytes([0x02, 0x0D, 0x14])   # MINT_POSITION, SETTLE_PAIR, SWEEP
_V4_COLLECT_SHAPE = bytes([0x01, 0x11])      # DECREASE_LIQUIDITY, TAKE_PAIR
_POOL_KEY = {"type": "tuple", "components": [
    {"type": "address"}, {"type": "address"}, {"type": "uint24"},
    {"type": "int24"}, {"type": "address"}]}
_MINT_PARAMS = [_POOL_KEY, {"type": "int24"}, {"type": "int24"}, {"type": "uint256"},
                {"type": "uint128"}, {"type": "uint128"}, {"type": "address"},
                {"type": "bytes"}]
_DECREASE_PARAMS = [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint128"},
                    {"type": "uint128"}, {"type": "bytes"}]
_PAIR = [{"type": "address"}, {"type": "address"}]
_TAKE_PAIR = [{"type": "address"}, {"type": "address"}, {"type": "address"}]
#: v4 PositionManager ``getPoolAndPositionInfo(uint256) returns (PoolKey, PositionInfo)``;
#: ``PositionInfo`` is a ``uint256`` user type.
POSM_POOL_AND_POSITION_INFO = {"name": "getPoolAndPositionInfo", "inputs": [{"type": "uint256"}],
                               "outputs": [_POOL_KEY, {"type": "uint256"}]}


def _raw(value) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return bytes.fromhex(str(value)[2:] if str(value).startswith("0x") else str(value))


def protocol_of(intent) -> str:
    """'v4' when the intent targets the chain's pinned v4 PositionManager,
    else 'v3'. Both are pins; nothing here is taken from the caller."""
    row = dex_registry.row_for(intent.chain, "v4")
    to = str(intent.to or "").lower()
    if row and row.position_manager and to == row.position_manager.lower():
        return "v4"
    return "v3"


def v4_pool_id(key) -> str:
    """``keccak(abi.encode(PoolKey))`` for a ``(c0, c1, fee, spacing, hooks)`` tuple."""
    from eth_utils import keccak
    return "0x" + keccak(abi.encode([_POOL_KEY], [tuple(key)])).hex()


def _v4_seed_split(tx):
    """``(mint calldata, seed)`` of the signed transaction. ``seed`` is None for a
    plain ``modifyLiquidities``; for the seed shape it is ``(key, sqrtPriceX96)``
    of the ``initializePool`` that runs first in the SAME transaction. A
    multicall of any other length or content, and a bare ``initializePool``,
    raise ValueError."""
    from core.wallet.abi import selector
    data = str(tx.get("data") or "").lower()
    init_sel = selector("initializePool((address,address,uint24,int24,address),uint160)")
    if data.startswith(init_sel):
        raise ValueError("a bare initializePool is refused: an initialised pool with no "
                         "liquidity has a free price; the seed initialises and mints in ONE "
                         "transaction")
    if not data.startswith(selector("multicall(bytes[])")):
        return data, None
    (inner,) = abi.decode([{"type": "bytes[]"}], "0x" + data[10:])
    calls = ["0x" + _raw(c).hex() for c in inner]
    if len(calls) != 2 or not calls[0].startswith(init_sel):
        raise ValueError("a v4 seed is exactly multicall([initializePool, modifyLiquidities]); "
                         "any other multicall is refused")
    key, sqrt_price = abi.decode([_POOL_KEY, {"type": "uint160"}], "0x" + calls[0][10:])
    return calls[1], (tuple(key), int(sqrt_price))


def _v4_unlock(tx):
    """``(actions, params)`` of the signed ``modifyLiquidities``; ValueError otherwise."""
    from core.wallet.abi import selector
    data = str(tx.get("data") or "")
    if not data.startswith(selector("modifyLiquidities(bytes,uint256)")):
        raise ValueError("a v4 liquidity transaction must call modifyLiquidities")
    unlock, _deadline = abi.decode([{"type": "bytes"}, {"type": "uint256"}], "0x" + data[10:])
    actions, params = abi.decode([{"type": "bytes"}, {"type": "bytes[]"}], _raw(unlock))
    return _raw(actions), params


def is_v4_collect(intent) -> bool:
    """The DECLARED v4 fee-collect shape: the pinned v4 PositionManager, a held
    position (``hold``), nothing paid in. The signed bytes are judged apart."""
    return (protocol_of(intent) == "v4" and intent.lp_position_effect == "hold"
            and not intent.lp_outflows)


def may_ride_account(intent) -> bool:
    """A token-bound account (``via_account``) may carry ONE liquidity shape:
    the v4 fee collect. Every other liquidity call through an account refuses."""
    return bool(intent.is_liquidity_op) and is_v4_collect(intent)


def decode_v4_collect(tx) -> dict:
    """The v4 exit, decoded from the transaction that will be signed: exactly
    DECREASE_LIQUIDITY(tokenId, L, min0, min1, "") + TAKE_PAIR(c0, c1, to).
    ``L == 0`` is the fee collect and carries minimums of 0. ``L > 0`` is a
    withdrawal (117 W7) and must carry a positive on-chain minimum: a decrease
    with no floor accepts any price. Raises ValueError on anything else."""
    actions, params = _v4_unlock(tx)
    if actions != _V4_COLLECT_SHAPE or len(params) != 2:
        raise ValueError("a v4 fee collect is exactly DECREASE_LIQUIDITY + TAKE_PAIR; "
                         "any other action sequence is refused")
    token_id, liquidity, min0, min1, hook_data = abi.decode(_DECREASE_PARAMS, _raw(params[0]))
    c0, c1, recipient = abi.decode(_TAKE_PAIR, _raw(params[1]))
    liquidity, min0, min1 = int(liquidity), int(min0), int(min1)
    if liquidity == 0 and (min0 or min1):
        raise ValueError("a v4 fee collect carries amount minimums of 0")
    if liquidity > 0 and min0 + min1 <= 0:
        raise ValueError(f"DECREASE_LIQUIDITY of {liquidity} is a withdrawal and must carry "
                         f"a positive on-chain minimum")
    if _raw(hook_data):
        raise ValueError("v4 hookData must be empty")
    return {"token_id": int(token_id), "currency0": str(c0).lower(),
            "currency1": str(c1).lower(), "recipient": str(recipient).lower(),
            "liquidity": liquidity, "min0": min0, "min1": min1}


def read_v4_position_key(rpc, chain, token_id) -> dict:
    """The PoolKey of v4 position *token_id* as the PINNED PositionManager
    reports it. An empty or unreadable answer raises (unknown is not a key)."""
    npm = dex_registry.resolve_position_manager(chain, "v4")
    spec = POSM_POOL_AND_POSITION_INFO
    raw = rpc("eth_call", [{"to": npm, "data": abi.encode_call(
        spec["name"], spec["inputs"], [int(token_id)])}, "latest"])
    if raw in (None, "", "0x"):
        raise ValueError(f"getPoolAndPositionInfo({token_id}) on {npm} returned nothing")
    (c0, c1, fee, spacing, hooks), _info = abi.decode(spec["outputs"], raw)
    return {"currency0": str(c0).lower(), "currency1": str(c1).lower(), "fee": int(fee),
            "tickSpacing": int(spacing), "hooks": str(hooks).lower()}


def read_v4_sqrt_price(rpc, chain, pool_id) -> int:
    """``sqrtPriceX96`` of *pool_id* from the PINNED PoolManager's own storage
    (``extsload`` of ``keccak(id, 6)``); 0 = not initialised. Unreadable raises."""
    from eth_utils import keccak
    pm = dex_registry.row_for(chain, "v4").pool_manager
    slot = keccak(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big"))
    raw = rpc("eth_call", [{"to": pm, "data": abi.encode_call(
        "extsload", [{"type": "bytes32"}], [slot])}, "latest"])
    if raw in (None, "", "0x"):
        raise ValueError(f"the v4 PoolManager returned nothing for pool {pool_id}")
    return int(str(raw), 16) & (2 ** 160 - 1)


def decode_v4_mint(tx) -> dict:
    """The v4 mint shape, decoded from the transaction that will be signed.
    Raises ValueError on anything else. ``seed_sqrt_price`` is the price the
    same transaction initialises the pool at (the seed shape), else None."""
    mint_data, seed = _v4_seed_split(tx)
    actions, params = _v4_unlock({"data": mint_data})
    if actions != _V4_MINT_SHAPE or len(params) != 3:
        raise ValueError("v4 builds exactly MINT_POSITION + SETTLE_PAIR + SWEEP; "
                         "any other action sequence is refused")
    key, lo, hi, liq, a0max, a1max, owner, hook_data = abi.decode(_MINT_PARAMS, _raw(params[0]))
    c0, c1, fee, spacing, hooks = key
    s0, s1 = abi.decode(_PAIR, _raw(params[1]))
    sweep_currency, sweep_to = abi.decode(_PAIR, _raw(params[2]))
    if _raw(hook_data):
        raise ValueError("v4 hookData must be empty")
    if (str(s0).lower(), str(s1).lower()) != (str(c0).lower(), str(c1).lower()):
        raise ValueError("SETTLE_PAIR must settle the minted PoolKey's two currencies")
    if str(sweep_currency).lower() != str(c0).lower() or str(sweep_to).lower() != str(owner).lower():
        raise ValueError("SWEEP must return currency0 to the position owner")
    if not int(liq) > 0:
        raise ValueError("v4 mint must add liquidity")
    from core.wallet import univ3_math as M
    lo, hi, spacing = int(lo), int(hi), int(spacing)
    if spacing < 1 or not M.MIN_TICK <= lo < hi <= M.MAX_TICK or lo % spacing or hi % spacing:
        raise ValueError(f"v4 ticks [{lo}, {hi}] must be ordered, inside the v4 bounds and "
                         f"multiples of the tick spacing {spacing}")
    m = {"currency0": str(c0).lower(), "currency1": str(c1).lower(), "fee": int(fee),
         "tickSpacing": spacing, "hooks": str(hooks).lower(), "tick_lower": lo,
         "tick_upper": hi, "liquidity": int(liq), "amount0_max": int(a0max),
         "amount1_max": int(a1max), "owner": str(owner).lower(), "seed_sqrt_price": None}
    m["pool_id"] = v4_pool_id((m["currency0"], m["currency1"], m["fee"], spacing, m["hooks"]))
    if seed is not None:
        s_key, sqrt_price = seed
        if v4_pool_id((str(s_key[0]).lower(), str(s_key[1]).lower(), int(s_key[2]),
                       int(s_key[3]), str(s_key[4]).lower())) != m["pool_id"]:
            raise ValueError("initializePool and MINT_POSITION must name the same PoolKey")
        if m["fee"] & V4_DYNAMIC_FEE_FLAG or m["fee"] > V4_MAX_LP_FEE:
            raise ValueError("a v4 seed needs a static fee; a dynamic-fee PoolKey is refused")
        if not M.sqrt_price_at_tick(M.MIN_TICK) <= sqrt_price < M.sqrt_price_at_tick(M.MAX_TICK):
            raise ValueError("the initial sqrtPriceX96 is outside the v4 price bounds")
        m["seed_sqrt_price"] = sqrt_price
    return m


def _assert_price_bound(m, sqrt_price, gap_bps, where):
    """Each signed maximum is at most what the signed liquidity needs at
    *sqrt_price*, plus *gap_bps*. With both maxima bound, the PositionManager
    reverts (MaximumAmountExceeded) at any pool price outside the band."""
    from core.wallet import univ3_math as M
    need0, need1 = M.amounts_for_liquidity(
        int(sqrt_price), M.sqrt_price_at_tick(m["tick_lower"]),
        M.sqrt_price_at_tick(m["tick_upper"]), m["liquidity"])
    for name, cap, need in (("amount0", m["amount0_max"], need0), ("amount1", m["amount1_max"], need1)):
        allowed = need * (10000 + gap_bps) // 10000 + 1
        if cap > allowed:
            raise ValueError(f"{name} maximum {cap} is more than the signed liquidity needs at "
                             f"{where} ({need}) plus {gap_bps} bps; the price is not bounded")


def _check_expected_events(intent, emitters):
    for emitter, topic in intent.expected_events:
        if emitter.lower() not in emitters:
            raise ValueError("required event emitter is not a pinned liquidity contract")
        if len(topic) != 66 or not topic.startswith("0x"):
            raise ValueError("required event topic must be bytes32")
        bytes.fromhex(topic[2:])


def _structural_v4_collect(intent, tx, holder, row, npm):
    """115 W4: the fee collect. The PoolKey and its hook are read in
    :func:`verify_pins` (they need the chain); everything else is here."""
    token_id = intent.lp_position[1] if intent.lp_position else None
    if (not intent.lp_position or intent.lp_position[0].lower() != npm
            or not isinstance(token_id, int) or isinstance(token_id, bool) or token_id < 0):
        raise ValueError("a v4 fee collect must declare the pinned v4 position manager and a position id")
    if not holder:
        raise ValueError("a v4 fee collect must know the holder it pays")
    c = decode_v4_collect(tx)
    if c["token_id"] != token_id:
        raise ValueError(f"DECREASE_LIQUIDITY names position {c['token_id']}, not the declared {token_id}")
    if c["recipient"] != str(holder).lower():
        raise ValueError(f"TAKE_PAIR pays {c['recipient']}, not the holder {holder} — refused")
    if int(tx.get("value") or 0) != 0:
        raise ValueError("a v4 fee collect sends no value")
    incoming = legs(intent.lp_inflows)
    if (len(intent.lp_inflows) != 2 or len(incoming) != 2 or set(incoming) != {
            None if cur == ZERO else cur for cur in (c["currency0"], c["currency1"])}):
        raise ValueError("a v4 fee collect declares exactly the two currencies TAKE_PAIR takes")
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in incoming.values()):
        raise ValueError("invalid liquidity leg amount")
    if c["liquidity"]:
        # 117 W7: a withdrawal. From the wallet only, with a declared positive
        # receipt and the PoolManager's event required.
        if intent.via_account:
            raise ValueError("a token-bound account changes v4 liquidity only to collect fees")
        if not any(n > 0 for n in incoming.values()):
            raise ValueError("a withdrawal must declare a positive minimum receipt; "
                             "a minimum of nothing asserts nothing")
        pm = row.pool_manager.lower()
        if (pm, TOPIC_V4_MODIFY_LIQUIDITY) not in {(e.lower(), t.lower())
                                                   for e, t in intent.expected_events}:
            raise ValueError("a v4 withdrawal must require the PoolManager's ModifyLiquidity event")
    _check_expected_events(intent, (npm, row.pool_manager.lower()))


def _structural_v4(intent, tx, holder=None):
    row = dex_registry.row_for(intent.chain, "v4")
    npm = dex_registry.resolve_position_manager(intent.chain, "v4").lower()
    if str(intent.to).lower() != npm or str(tx.get("to")).lower() != npm:
        raise ValueError("liquidity transaction and intent must target the pinned v4 position manager")
    if tx.get("chainId") is not None and int(tx["chainId"]) != row.chain_id:
        raise ValueError("liquidity transaction chainId does not match the pinned chain")
    if is_v4_collect(intent):
        return _structural_v4_collect(intent, tx, holder, row, npm)
    if intent.via_account:
        raise ValueError("a token-bound account changes v4 liquidity only to collect fees")
    if intent.lp_position_effect != "mint" or intent.lp_inflows:
        raise ValueError("v4 builds a new-position MINT only, a fee COLLECT and a "
                         "withdrawal that keeps the position; increase and burn are refused")
    if not intent.lp_position or intent.lp_position[0].lower() != npm or intent.lp_position[1] is not None:
        raise ValueError("liquidity must declare the pinned v4 position manager and an unknown id")
    m = decode_v4_mint(tx)
    if not dex_registry.hook_pinned(intent.chain, m["hooks"]):
        raise ValueError(f"v4 hook {m['hooks']} has no pinned code hash; an unreviewed hook runs inside the add")
    if holder and m["owner"] != str(holder).lower():
        raise ValueError(f"the position is minted to {m['owner']}, not the holder {holder} — refused")
    seeded = m["seed_sqrt_price"] is not None
    if seeded:
        _assert_price_bound(m, m["seed_sqrt_price"], SEED_MAX_GAP_BPS, "the signed initial price")
    declares_init = (row.pool_manager.lower(), TOPIC_V4_INITIALIZE) in {
        (e.lower(), t.lower()) for e, t in intent.expected_events}
    if seeded != declares_init:
        raise ValueError("the PoolManager's Initialize event is required by a seed and by "
                         "nothing else")
    out = legs(intent.lp_outflows)
    if len(intent.lp_outflows) > 2 or len(out) != len(intent.lp_outflows):
        raise ValueError("liquidity requires at most two distinct legs per direction")
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in out.values()):
        raise ValueError("invalid liquidity leg amount")
    currencies = {None if c == ZERO else c for c in (m["currency0"], m["currency1"])}
    if set(out) - currencies:
        raise ValueError("a declared liquidity leg is not a currency of the minted PoolKey")
    for cur, cap in ((m["currency0"], m["amount0_max"]), (m["currency1"], m["amount1_max"])):
        declared = out.get(None if cur == ZERO else cur, 0)
        if cap > declared:
            raise ValueError(f"calldata may pay {cap} of {cur} but the intent declares {declared}")
    if int(tx.get("value") or 0) != out.get(None, 0):
        raise ValueError("native liquidity leg must equal tx.value")
    pm = row.pool_manager.lower()
    if (pm, TOPIC_V4_MODIFY_LIQUIDITY) not in {(e.lower(), t.lower()) for e, t in intent.expected_events}:
        raise ValueError("a v4 mint must require the PoolManager's ModifyLiquidity event")
    _check_expected_events(intent, (npm, pm))


def verify_pins(rpc, intent, tx):
    """The guard's pin step: re-hash every pinned contract the protocol uses,
    and for v4 the hook the signed PoolKey names (090 R0.4). A v4 collect
    signs no PoolKey: the key is READ from the pinned PositionManager for the
    declared position, TAKE_PAIR must take exactly its two currencies, and its
    hook (which runs inside the zero decrease) must be pinned."""
    protocol = protocol_of(intent)
    dex_registry.verify_pins(rpc, intent.chain, protocol)
    if protocol != "v4":
        return
    if not is_v4_collect(intent):
        m = decode_v4_mint(tx)
        dex_registry.verify_hook(rpc, intent.chain, m["hooks"])
        sqrt_price = read_v4_sqrt_price(rpc, intent.chain, m["pool_id"])
        if m["seed_sqrt_price"] is None:
            if sqrt_price == 0:
                raise ValueError(f"v4 pool {m['pool_id']} is not initialised; a mint into it "
                                 f"needs the seed shape")
            _assert_price_bound(m, sqrt_price, MINT_MAX_GAP_BPS, "the pool price")
        elif sqrt_price != 0:
            raise ValueError(f"v4 pool {m['pool_id']} is already initialised; a seed would "
                             f"mint at a price it did not set")
        return
    c = decode_v4_collect(tx)
    key = read_v4_position_key(rpc, intent.chain, c["token_id"])
    if (key["currency0"], key["currency1"]) != (c["currency0"], c["currency1"]):
        raise ValueError(f"TAKE_PAIR takes {c['currency0']}/{c['currency1']}, but position "
                         f"#{c['token_id']} is on {key['currency0']}/{key['currency1']}")
    if not dex_registry.hook_pinned(intent.chain, key["hooks"]):
        raise ValueError(f"v4 hook {key['hooks']} has no pinned code hash; an unreviewed hook "
                         f"runs inside the collect")
    dex_registry.verify_hook(rpc, intent.chain, key["hooks"])


def permit2_grant_declared(intent, tx, permit2, token, spender, amount) -> bool:
    """True only for the ONE Permit2 grant a verb may author: an allowance op
    whose signed transaction is ``Permit2.approve(token, v4 PosM, amount,
    expiration)`` on the pinned Permit2, declared in
    ``expected_allowance_grants`` (so it is priced like any grant), to the
    pinned v4 PositionManager, living at most PERMIT2_GRANT_MAX_TTL_S. The
    emitted grant must be exactly the signed one."""
    try:
        if not intent.is_allowance_op or intent.via_account:
            return False
        row = dex_registry.row_for(intent.chain, "v4")
        if row is None or not row.permit2 or not row.position_manager:
            return False
        p2, npm = row.permit2.lower(), row.position_manager.lower()
        if str(permit2).lower() != p2 or str(tx.get("to") or "").lower() != p2:
            return False
        if str(spender).lower() != npm or str(intent.to or "").lower() != npm:
            return False
        data = str(tx.get("data") or "")
        from core.wallet.abi import selector
        if not data.startswith(selector("approve(address,address,uint160,uint48)")):
            return False
        c_token, c_spender, c_amount, c_exp = abi.decode(
            [{"type": "address"}, {"type": "address"}, {"type": "uint160"}, {"type": "uint48"}],
            "0x" + data[10:])
        if (str(c_token).lower(), str(c_spender).lower(), int(c_amount)) != (
                str(token).lower(), npm, int(amount)):
            return False
        now = int(time.time())
        if not now < int(c_exp) <= now + PERMIT2_GRANT_MAX_TTL_S:
            return False
        declared = {(t.lower(), s.lower()): a for t, s, a in intent.expected_allowance_grants}
        return 0 < int(amount) <= declared.get((str(token).lower(), npm), 0)
    except Exception:
        return False


def legs(items):
    return {(token.lower() if token else None): amount for token, amount in items}


def _tup(*types):
    return {"type": "tuple", "components": [{"type": t} for t in types]}


#: Every v3 NonfungiblePositionManager call a liquidity verb builds, and nothing
#: else. The SIGNED calldata is judged, not the declaration: a collect that pays
#: someone else, a decrease with no on-chain floor, or a position transfer riding
#: under an LP intent is refused before the simulation (which a contract can tell
#: apart from the real block).
_V3_CALLS = {
    "mint": [_tup("address", "address", "uint24", "int24", "int24", "uint256", "uint256",
                  "uint256", "uint256", "address", "uint256")],
    "increaseLiquidity": [_tup("uint256", "uint256", "uint256", "uint256", "uint256", "uint256")],
    "decreaseLiquidity": [_tup("uint256", "uint128", "uint256", "uint256", "uint256")],
    "collect": [_tup("uint256", "address", "uint128", "uint128")],
    "burn": [{"type": "uint256"}],
    "refundETH": [],
    "createAndInitializePoolIfNecessary": [{"type": "address"}, {"type": "address"},
                                           {"type": "uint24"}, {"type": "uint160"}],
}
_V3_DEPOSIT_CALLS = {"mint", "increaseLiquidity", "refundETH", "createAndInitializePoolIfNecessary"}
_V3_EXIT_CALLS = {"decreaseLiquidity", "collect", "burn"}


def decode_v3_calls(tx):
    """``[(name, args), ...]`` for a v3 position-manager transaction — one call
    or one flat ``multicall(bytes[])``. Raises ValueError on any other shape."""
    data = str(tx.get("data") or tx.get("input") or "").lower()
    if not data.startswith("0x") or len(data) < 10:
        raise ValueError("a v3 liquidity transaction must carry position-manager calldata")
    by_selector = {abi.selector(abi.signature_of(n, i)): n for n, i in _V3_CALLS.items()}
    if data[:10] == abi.selector("multicall(bytes[])"):
        (inner,) = abi.decode([{"type": "bytes[]"}], "0x" + data[10:])
        calls = ["0x" + _raw(c).hex() for c in inner]
    else:
        calls = [data]
    out = []
    for call in calls:
        name = by_selector.get(call[:10])
        if name is None:
            raise ValueError(f"v3 liquidity calldata calls {call[:10]}, which no liquidity verb builds")
        out.append((name, abi.decode(_V3_CALLS[name], "0x" + call[10:]) if _V3_CALLS[name] else ()))
    if not out:
        raise ValueError("a v3 liquidity multicall must carry at least one call")
    return out


def _structural_v3_calldata(intent, tx, holder):
    """Bind the signed v3 calldata to the declared position, the wallet and the
    declared direction (WAL-2): a fee collection pays the wallet and nobody else."""
    calls = decode_v3_calls(tx)
    names = [n for n, _ in calls]
    deposit = bool(intent.lp_outflows)
    allowed = _V3_DEPOSIT_CALLS if deposit else _V3_EXIT_CALLS
    if set(names) - allowed:
        raise ValueError(f"a liquidity {'deposit' if deposit else 'withdrawal'} may not call "
                         f"{sorted(set(names) - allowed)}")
    token_id = intent.lp_position[1]
    me = str(holder or "").lower()
    for name, args in calls:
        p = args[0] if args else None
        if name in ("increaseLiquidity", "decreaseLiquidity", "collect", "burn"):
            if (int(p) if name == "burn" else int(p[0])) != token_id:
                raise ValueError(f"{name} names position {p if name == 'burn' else p[0]}, "
                                 f"not the declared {token_id}")
        if name == "mint" and me and str(p[9]).lower() != me:
            raise ValueError("mint must mint the position to the wallet itself")
        if name == "collect" and me and str(p[1]).lower() != me:
            raise ValueError(f"collect pays {p[1]}, not the wallet — refused")
        if name == "decreaseLiquidity" and (int(p[1]) <= 0 or int(p[2]) + int(p[3]) <= 0):
            raise ValueError("decreaseLiquidity must remove liquidity with a positive on-chain minimum")
    if deposit:
        if names.count("mint") + names.count("increaseLiquidity") != 1:
            raise ValueError("a deposit makes exactly one mint or increaseLiquidity call")
        if ("mint" in names) != (intent.lp_position_effect == "mint"):
            raise ValueError("the deposit call does not match the declared position effect")
        return
    if names.count("collect") != 1:
        raise ValueError("a withdrawal must collect exactly once, to the wallet")
    if ("burn" in names) != (intent.lp_position_effect == "burn"):
        raise ValueError("burn in the calldata does not match the declared position effect")
    if not any(n > 0 for _, n in intent.lp_inflows):
        raise ValueError("a withdrawal must declare a positive minimum receipt; "
                         "a minimum of nothing asserts nothing")


def structural(intent, tx, holder=None):
    if any((intent.is_deploy, intent.is_claim, intent.is_registration,
            intent.is_allowance_op, intent.is_nft_op)):
        raise ValueError("liquidity cannot be combined with another intent shape")
    if (intent.token or intent.amount_raw or intent.nft_out or intent.expected_nft_in
            or intent.expected_allowance_grants or intent.nft_operator_ops
            or intent.inflow_token or intent.min_native_inflow_wei is not None
            or intent.min_inflow_raw is not None):
        raise ValueError("liquidity must declare its effects only in LP fields")
    if protocol_of(intent) == "v4":
        return _structural_v4(intent, tx, holder)
    row = dex_registry.row_for(intent.chain, "v3")
    npm = dex_registry.resolve_position_manager(intent.chain, "v3").lower()
    if (str(intent.to).lower() != npm or str(tx.get("to")).lower() != npm):
        raise ValueError("liquidity transaction and intent must target the pinned v3 position manager")
    if tx.get("chainId") is not None and int(tx["chainId"]) != row.chain_id:
        raise ValueError("liquidity transaction chainId does not match the pinned chain")
    if not (intent.lp_outflows or intent.lp_inflows or intent.expected_events):
        raise ValueError("liquidity must assert an outflow, inflow, or required event")
    for values, minimum in ((intent.lp_outflows, 1), (intent.lp_inflows, 0)):
        if len(values) > 2 or len(legs(values)) != len(values):
            raise ValueError("liquidity requires at most two distinct legs per direction")
        if any(not isinstance(n, int) or isinstance(n, bool) or n < minimum for _, n in values):
            raise ValueError("invalid liquidity leg amount")
    if legs(intent.lp_outflows).keys() & legs(intent.lp_inflows).keys():
        raise ValueError("a liquidity leg cannot declare both directions")
    native_max = legs(intent.lp_outflows).get(None, 0)
    if int(tx.get("value") or 0) != native_max:
        raise ValueError("native liquidity leg must equal tx.value")
    if not intent.lp_position or intent.lp_position[0].lower() != npm:
        raise ValueError("liquidity must declare the pinned position manager")
    token_id = intent.lp_position[1]
    effect = intent.lp_position_effect
    if effect not in ("mint", "hold", "burn"):
        raise ValueError("unknown liquidity position effect")
    if effect == "mint":
        if token_id is not None or not intent.lp_outflows:
            raise ValueError("mint requires an unknown id and a deposit")
    elif not isinstance(token_id, int) or isinstance(token_id, bool) or token_id < 0:
        raise ValueError("hold/burn requires a position id")
    if effect == "burn" and (intent.lp_outflows or not any(n > 0 for _, n in intent.lp_inflows)):
        raise ValueError("burn requires a withdrawal with positive receipts; standalone burn is unsupported")
    for emitter, topic in intent.expected_events:
        if emitter.lower() not in (npm, row.factory.lower()):
            raise ValueError("required event emitter is not a pinned liquidity contract")
        if len(topic) != 66 or not topic.startswith("0x"):
            raise ValueError("required event topic must be bytes32")
        bytes.fromhex(topic[2:])
    _structural_v3_calldata(intent, tx, holder)


def assert_deltas(intent, deltas, dust, tx=None):
    outgoing, incoming = legs(intent.lp_outflows), legs(intent.lp_inflows)
    measured = {t.lower(): n for t, n in deltas.token_deltas.items()}
    measured[None] = deltas.native_delta
    for token, maximum in outgoing.items():
        moved = measured.get(token)
        if moved is None or not 0 < -moved <= maximum:
            raise ValueError(f"liquidity outflow {token}: measured {moved}, expected 0 < outflow <= {maximum}")
    for token, minimum in incoming.items():
        moved = measured.get(token)
        if moved is None or moved < minimum:
            raise ValueError(f"liquidity inflow {token}: measured {moved}, below minimum {minimum}")
    for token, moved in measured.items():
        if token not in outgoing and token not in incoming and abs(moved) > (dust if token is None else 0):
            raise ValueError(f"UNDECLARED liquidity token delta: {token} {moved}")
    npm, token_id = intent.lp_position
    effect = intent.lp_position_effect
    ins, outs = deltas.holder_nft_in, deltas.holder_nft_out
    minted_id = None
    if effect == "mint":
        if (len(ins) != 1 or outs or ins[0][0].lower() != npm.lower()
                or ins[0][1] != "erc721" or ins[0][2].lower() != ZERO or ins[0][4] != 1):
            raise ValueError("liquidity mint must mint exactly ONE position NFT from the pinned manager")
        minted_id = int(ins[0][3])
    elif effect == "hold":
        if ins or outs:
            raise ValueError("liquidity hold must not transfer any NFT")
    elif (ins or len(outs) != 1 or
          tuple(outs[0]) != (npm.lower(), "erc721", ZERO, token_id, 1)):
        raise ValueError("liquidity burn must destroy exactly the declared position NFT")
    observed = {(a.lower(), t.lower()) for a, t in deltas.event_topics}
    for emitter, topic in intent.expected_events:
        if (emitter.lower(), topic.lower()) not in observed:
            raise ValueError(f"required event {topic} was not emitted by {emitter}")
    assert_position_events(intent, deltas.logs, minted_id if effect == "mint" else token_id, tx)
    return minted_id


def assert_position_events(intent, logs, token_id, tx=None):
    """A pinned event about SOMEBODY ELSE's position is not our receipt. With
    the signed *tx*, a v4 mint's events must also equal the signed bytes."""
    if protocol_of(intent) == "v4":
        return _assert_v4_position_events(intent, logs, token_id, tx)
    from eth_utils import keccak
    signatures = {
        "increase": "IncreaseLiquidity(uint256,uint128,uint256,uint256)",
        "decrease": "DecreaseLiquidity(uint256,uint128,uint256,uint256)",
        "collect": "Collect(uint256,address,uint256,uint256)",
    }
    topics = {"0x" + keccak(text=sig).hex(): name for name, sig in signatures.items()}
    seen = set()
    for log in logs:
        ts = log.get("topics", [])
        if str(log.get("address", "")).lower() != intent.to.lower() or not ts:
            continue
        kind = topics.get(str(ts[0]).lower())
        if kind is None:
            continue
        if len(ts) != 2 or int(ts[1], 16) != token_id:
            raise ValueError("liquidity event concerns a different position id")
        raw = bytes.fromhex(log.get("data", "")[2:])
        if len(raw) != 96:
            raise ValueError("malformed liquidity position event")
        if kind in ("increase", "decrease") and int.from_bytes(raw[:32], "big") <= 0:
            raise ValueError("liquidity position event reports no liquidity change")
        seen.add(kind)
    required = "increase" if intent.lp_outflows else "collect" if intent.lp_inflows else "decrease"
    if required not in seen:
        raise ValueError(f"missing {required} event for the declared position id")
    if intent.lp_outflows and ("decrease" in seen or "collect" in seen):
        raise ValueError("a deposit may not also withdraw liquidity")


def price_outflows(intent, deltas, price_fn, fallback_price_fn, decimals_fn):
    from core.wallet import chains
    row = chains.get(intent.chain)
    held = legs(intent.lp_held_balances)
    measured = {t.lower(): n for t, n in deltas.token_deltas.items()}
    measured[None] = deltas.native_delta
    values, raws, prices, decs = [], [], [], []
    for token, _maximum in intent.lp_outflows:
        key = token.lower() if token else None
        raw = -measured[key]
        asset = token or row.wrapped_native
        def quoted(fn):
            try:
                p = fn(intent.chain, asset) if fn else None
                return p if p is not None and isfinite(p) and p > 0 else None
            except Exception:
                return None
        price = quoted(price_fn)
        if price is None and raw <= held.get(key, -1):
            price = quoted(fallback_price_fn)
        decimals = decimals_fn(intent.chain, token) if token else row.native_decimals
        if decimals is None:
            raise ValueError("liquidity token decimals unknown")
        values.append(None if price is None else raw / 10 ** decimals * price)
        raws.append(raw); prices.append(price); decs.append(decimals)
    implied = any(v is None for v in values)
    seeded = _seed_values(intent, deltas, values, raws, prices, decs)
    if seeded is not None:
        return sum(seeded), implied
    if implied:
        # Only two-sided deposits permit the paired-leg valuation.
        known = [v for v in values if v is not None]
        if len(values) != 2 or len(known) != 1:
            raise ValueError("unpriceable liquidity outflow; booking it at $0.00 would widen every other cap")
        # CR-L13: giving the unpriceable leg the priced leg's USD assumes the
        # pool's ratio is a fair price. In an EXISTING pool that ratio is
        # whatever its last trader seeded — the unpriceable leg can be worth
        # far more than its paired leg, uncapped. Only a pool THIS transaction
        # creates carries a ratio we chose (initial_price), so only there does
        # the paired valuation describe the deposit.
        if not creates_pool(intent):
            raise ValueError(
                "one liquidity leg has no trustworthy price, and this pool "
                "already exists, so its ratio is not a valuation; refusing "
                "rather than giving that leg the priced leg's USD")
        values = [known[0] if v is None else v for v in values]
    return sum(values), implied


def creates_pool(intent):
    """True when the intent REQUIRES the event that creates its pool: the pinned
    v3 factory's PoolCreated, or the pinned v4 PoolManager's Initialize (117)."""
    from eth_utils import keccak
    topic = "0x" + keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
    row = dex_registry.row_for(intent.chain, "v3")
    factory = (getattr(row, "factory", "") or "").lower()
    v4 = dex_registry.row_for(intent.chain, "v4")
    pm = (getattr(v4, "pool_manager", "") or "").lower()
    wanted = {(factory, topic), (pm, TOPIC_V4_INITIALIZE)} - {("", topic), ("", TOPIC_V4_INITIALIZE)}
    return any((str(e).lower(), str(t).lower()) in wanted for e, t in intent.expected_events)


def _v4_initialize_log(intent, logs):
    """``(pool id, currency0, currency1, sqrtPriceX96)`` of the ONE Initialize the
    pinned PoolManager emitted, or None when it emitted none. Two raise."""
    row = dex_registry.row_for(intent.chain, "v4")
    pm = (getattr(row, "pool_manager", "") or "").lower()
    found = []
    for log in logs or ():
        ts = log.get("topics", [])
        if (not pm or str(log.get("address", "")).lower() != pm or not ts
                or str(ts[0]).lower() != TOPIC_V4_INITIALIZE):
            continue
        raw = bytes.fromhex(str(log.get("data", ""))[2:])
        if len(ts) != 4 or len(raw) != 160:
            raise ValueError("malformed Initialize event")
        found.append((str(ts[1]).lower(), "0x" + str(ts[2])[-40:].lower(),
                      "0x" + str(ts[3])[-40:].lower(), int.from_bytes(raw[96:128], "big")))
    if len(found) > 1:
        raise ValueError("more than one pool was initialised in this transaction")
    return found[0] if found else None


def _seed_values(intent, deltas, values, raws, prices, decimals):
    """117: the legs of a pool THIS transaction creates on v4. A leg with no
    price of its own is worth its amount through the price the pool was
    initialised at (the signed one: the event assertions bind it), in the
    priced leg's currency; a leg that has both takes the larger. A native leg is
    the anchor: it needs its own price and is never revalued. None when the
    transaction initialises no v4 pool."""
    init = _v4_initialize_log(intent, getattr(deltas, "logs", None))
    if init is None or len(values) != 2:
        return None
    _pid, c0, _c1, sqrt_price = init
    keys = [(t.lower() if t else ZERO) for t, _ in intent.lp_outflows]
    out = list(values)
    native = [k == ZERO for k in keys]
    if any(native) and prices[native.index(True)] is None:
        raise ValueError("a seed needs a trustworthy price for its native leg")
    for i, j in ((0, 1), (1, 0)):
        # The native leg is the anchor: it keeps its own price and values the other.
        if prices[j] is None or sqrt_price <= 0 or native[i]:
            continue
        # raw of leg i expressed as raw of leg j at P = currency1 per currency0
        num, den = (sqrt_price ** 2, 2 ** 192) if keys[i] == c0 else (2 ** 192, sqrt_price ** 2)
        through = raws[i] * num / den / 10 ** decimals[j] * prices[j]
        out[i] = through if out[i] is None else max(out[i], through)
    if any(v is None for v in out):
        raise ValueError("a seed needs a trustworthy price for at least one leg")
    return out


def _v4_position_deltas(intent, logs, token_id):
    """The liquidity deltas of every PoolManager ModifyLiquidity whose sender is
    the pinned v4 PositionManager; one about another position id raises."""
    row = dex_registry.row_for(intent.chain, "v4")
    pm, npm = row.pool_manager.lower(), row.position_manager.lower()
    ours = []
    for log in logs:
        ts = log.get("topics", [])
        if (str(log.get("address", "")).lower() != pm or not ts
                or str(ts[0]).lower() != TOPIC_V4_MODIFY_LIQUIDITY):
            continue
        if len(ts) != 3:
            raise ValueError("malformed ModifyLiquidity event")
        if int(ts[2], 16) != int(npm, 16):
            continue                     # a hook's own modification, not the position's
        raw = bytes.fromhex(str(log.get("data", ""))[2:])
        if len(raw) != 128:
            raise ValueError("malformed ModifyLiquidity event")
        delta = int.from_bytes(raw[64:96], "big", signed=True)
        salt = int.from_bytes(raw[96:128], "big")
        if salt != int(token_id):
            raise ValueError("liquidity event concerns a different position id")
        ours.append(delta)
    return ours


def _assert_v4_signed_events(intent, logs, token_id, tx):
    """117: the events of a v4 mint equal the SIGNED bytes. Our ModifyLiquidity
    names the signed pool id, ticks and liquidity; a seed's Initialize names the
    same pool id and the signed price; a plain mint initialises nothing."""
    m = decode_v4_mint(tx)
    row = dex_registry.row_for(intent.chain, "v4")
    pm, npm = row.pool_manager.lower(), row.position_manager.lower()
    signed32 = lambda b: int.from_bytes(b, "big", signed=True)
    for log in logs:
        ts = log.get("topics", [])
        if (str(log.get("address", "")).lower() != pm or len(ts) != 3
                or str(ts[0]).lower() != TOPIC_V4_MODIFY_LIQUIDITY or int(ts[2], 16) != int(npm, 16)):
            continue
        raw = bytes.fromhex(str(log.get("data", ""))[2:])
        if int.from_bytes(raw[96:128], "big") != int(token_id):
            continue
        if str(ts[1]).lower() != m["pool_id"]:
            raise ValueError("ModifyLiquidity names a pool other than the signed PoolKey")
        if (signed32(raw[0:32]), signed32(raw[32:64])) != (m["tick_lower"], m["tick_upper"]):
            raise ValueError("ModifyLiquidity names ticks other than the signed ones")
        if signed32(raw[64:96]) != m["liquidity"]:
            raise ValueError("ModifyLiquidity adds a liquidity other than the signed one")
    init = _v4_initialize_log(intent, logs)
    if m["seed_sqrt_price"] is None:
        if init is not None:
            raise ValueError("a plain v4 mint may not initialise a pool")
        return
    if init is None or init[0] != m["pool_id"] or init[3] != m["seed_sqrt_price"]:
        raise ValueError("the seed's Initialize event does not name the signed PoolKey and price")


def _assert_v4_position_events(intent, logs, token_id, tx=None):
    """Exactly one PoolManager ModifyLiquidity whose sender is the pinned v4
    PositionManager, salted with OUR minted id, adding liquidity.

    A withdrawal (117 W7, known only with the signed *tx*): exactly one, about
    OUR position, removing exactly the signed liquidity.

    A collect (115 W4): at most one, about OUR position, with a liquidity change
    of exactly 0 — a non-zero change is a deposit or a withdrawal. Its PRESENCE
    is not required: no test in this tree or the contract repo shows the event
    of a zero decrease, and the collect is bound by its signed calldata, the
    owner read and the measured deltas (inflows only, no NFT moved)."""
    ours = _v4_position_deltas(intent, logs, token_id)
    removed = decode_v4_collect(tx)["liquidity"] if (tx is not None and is_v4_collect(intent)) else 0
    if removed:
        if ours != [-removed]:
            raise ValueError(f"a v4 withdrawal of {removed} must show exactly one "
                             f"ModifyLiquidity of -{removed} for position {token_id}; saw {ours}")
        return
    if is_v4_collect(intent):
        if any(d != 0 for d in ours):
            raise ValueError("a v4 fee collect may not change the position's liquidity")
        if len(ours) > 1:
            raise ValueError(f"expected at most one ModifyLiquidity for position {token_id}, "
                             f"saw {len(ours)}")
        return
    if any(d <= 0 for d in ours):
        raise ValueError("a v4 deposit may not withdraw liquidity")
    if len(ours) != 1:
        raise ValueError(f"expected exactly one ModifyLiquidity for position {token_id}, saw {len(ours)}")
    if tx is not None:
        _assert_v4_signed_events(intent, logs, token_id, tx)
