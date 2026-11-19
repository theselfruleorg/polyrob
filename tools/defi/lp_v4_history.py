"""118 W3: what a v4 pool's own logs say about a position and its pool.

Three reads, all from the pinned PoolManager's logs and the state the caller
already holds — no indexer, no archive call:

* the RESULT of a position against holding its deposits: every deposit and
  withdrawal is a ``ModifyLiquidity`` log with the position's salt, and the
  pool price at that log is the ``sqrtPriceX96`` of the last ``Swap`` before it
  (or the ``Initialize`` price) — a v4 price moves by nothing else;
* the pool's DEPTH: the currency0 that moves the price 2% and 10%;
* the pool's liquidity HOLDERS, by position, with each one's in-range share.

A read that fails raises ``LpReadError``; the ``*_lines`` renders turn that into
one NOT READ line. Nothing here is "0" for a read that failed.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.wallet import dex_registry, univ3_math as M
from tools.defi import lp_v4 as V
from tools.defi.lp_reads import LpReadError, view

#: The widest log query a public node allows is 10 M blocks; the walk back from
#: the head stops at the pool's ``Initialize`` or after this many windows.
LOG_WINDOW = 9_000_000
LOG_WINDOWS = 16
#: The first window searched for the swap before a liquidity change.
_SWAP_WINDOW = 100_000
#: Positions named in the holder line.
_TOP = 3


def topic_initialize() -> str:
    from eth_utils import keccak
    return "0x" + keccak(
        text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()


@dataclass
class Change:
    block: int
    index: int
    sender: str
    tick_lower: int
    tick_upper: int
    delta: int
    salt: int


@dataclass
class PoolHistory:
    init_block: int
    init_sqrt: int
    changes: List[Change]


def _words(log) -> List[int]:
    data = bytes.fromhex(str(log.get("data", "0x"))[2:])
    return [int.from_bytes(data[i:i + 32], "big") for i in range(0, len(data), 32)]


def _at(log) -> Tuple[int, int]:
    return int(str(log["blockNumber"]), 16), int(str(log.get("logIndex", "0x0")), 16)


def _logs(rpc, manager: str, lo: int, hi: int, topic: str, pool_id: str) -> list:
    return rpc("eth_getLogs", [{"address": manager, "fromBlock": hex(lo), "toBlock": hex(hi),
                                "topics": [topic, pool_id]}]) or []


def _manager(chain: str) -> str:
    row = dex_registry.row_for(chain, "v4")
    if row is None or not row.pool_manager:
        raise LpReadError(f"chain {chain!r} has no pinned Uniswap v4 deployment")
    return row.pool_manager


def pool_history(rpc, chain: str, pool_id: str) -> PoolHistory:
    """Every liquidity change of one pool since its ``Initialize``, oldest
    first. Raises when the pool's start is older than the log walk reaches —
    a partial history would under-count the deposits."""
    manager = _manager(chain)
    hi = int(rpc("eth_blockNumber", []), 16)
    raw: list = []
    for _ in range(LOG_WINDOWS):
        if hi < 0:
            break
        lo = max(0, hi - LOG_WINDOW)
        raw = _logs(rpc, manager, lo, hi, V.TOPIC_MODIFY_LIQUIDITY, pool_id) + raw
        init = _logs(rpc, manager, lo, hi, topic_initialize(), pool_id)
        if init:
            words = _words(init[0])
            if len(words) < 5:
                raise LpReadError("the Initialize log has an unexpected layout")
            start = _at(init[0])
            changes = []
            for log in raw:
                w, topics = _words(log), log.get("topics") or []
                if len(w) < 4 or len(topics) < 3:
                    raise LpReadError("a ModifyLiquidity log has an unexpected layout")
                block, index = _at(log)
                changes.append(Change(block, index, "0x" + str(topics[2])[-40:],
                                      V._signed(w[0]), V._signed(w[1]), V._signed(w[2]), w[3]))
            changes.sort(key=lambda c: (c.block, c.index))
            return PoolHistory(start[0], words[3], changes)
        hi = lo - 1
    raise LpReadError("the pool's Initialize log is older than the log walk reaches")


def price_at(rpc, chain: str, pool_id: str, hist: PoolHistory, change: Change) -> int:
    """The pool's ``sqrtPriceX96`` when *change* happened: the price after the
    last swap before it, or the ``Initialize`` price when no swap came first."""
    manager = _manager(chain)
    hi, span = change.block, _SWAP_WINDOW
    while hi >= hist.init_block:
        lo = max(hist.init_block, hi - span)
        before = [log for log in _logs(rpc, manager, lo, hi, V.TOPIC_SWAP, pool_id)
                  if _at(log) < (change.block, change.index)]
        if before:
            words = _words(max(before, key=_at))
            if len(words) < 6:
                raise LpReadError("a Swap log has an unexpected layout")
            return words[2]
        hi, span = lo - 1, min(span * 10, LOG_WINDOW)
    return hist.init_sqrt


def position_flows(rpc, chain: str, pos, hist: PoolHistory) -> Dict[str, Any]:
    """Raw ``in0/in1`` (deposited) and ``out0/out1`` (withdrawn) of one position,
    each change valued at the pool price it happened at."""
    row = dex_registry.row_for(chain, "v4")
    manager = str(row.position_manager or "").lower()
    lower, upper = M.sqrt_price_at_tick(pos.tick_lower), M.sqrt_price_at_tick(pos.tick_upper)
    flows = {"in0": 0, "in1": 0, "out0": 0, "out1": 0, "changes": 0}
    net = 0
    for c in hist.changes:
        if (c.salt != int(pos.token_id) or c.sender.lower() != manager or not c.delta
                or (c.tick_lower, c.tick_upper) != (pos.tick_lower, pos.tick_upper)):
            continue
        a0, a1 = M.amounts_for_liquidity(price_at(rpc, chain, pos.pool_id, hist, c),
                                         lower, upper, abs(c.delta))
        side = "in" if c.delta > 0 else "out"
        flows[side + "0"] += a0
        flows[side + "1"] += a1
        flows["changes"] += 1
        net += c.delta
    if not flows["changes"]:
        raise LpReadError("no liquidity change of this position is in the pool's logs")
    if net != int(pos.liquidity):
        raise LpReadError(f"the logs sum to liquidity {net}, the position holds {pos.liquidity}")
    return flows


def _in0(raw1: int, sqrt_price_x96: int) -> int:
    """Raw currency1 valued in raw currency0 at the pool price."""
    return int(raw1) * M.Q96 * M.Q96 // (int(sqrt_price_x96) ** 2)


def result_lines(rpc, chain: str, pos, st, held: Tuple[int, int],
                 fees: Optional[Tuple[int, int]], dec0: Optional[int],
                 dec1: Optional[int]) -> list:
    """The position's result against holding its deposits, in currency0."""
    try:
        f = position_flows(rpc, chain, pos, pool_history(rpc, chain, pos.pool_id))
    except Exception as exc:
        return [f"  result against holding: NOT READ ({type(exc).__name__}: {str(exc)[:100]})"]
    amt, s = V._raw_text, st.sqrt_price_x96
    f0, f1 = fees if fees is not None else (0, 0)
    path = held[0] + f0 + f["out0"] + _in0(held[1] + f1 + f["out1"], s)
    hold = f["in0"] + _in0(f["in1"], s)
    diff = path - hold
    pct = f" ({diff / hold * 100:+.1f}%)" if hold else ""
    sign = "-" if diff < 0 else "+"
    # Each sum has its name: an agent read the first one as "value now" (prod 2026-11-18).
    now = held[0] + f0 + _in0(held[1] + f1, s)
    parts = "the position now + what was withdrawn" if f["out0"] or f["out1"] else "the position now"
    parts += " + the fees owed" if fees is not None else ""
    lines = [
        f"  deposited: {amt(f['in0'], dec0)} currency0 + {amt(f['in1'], dec1)} currency1"
        + (f"; withdrawn: {amt(f['out0'], dec0)} + {amt(f['out1'], dec1)}"
           if f["out0"] or f["out1"] else "")
        + f" ({f['changes']} change(s), each at the pool price of its block)",
        f"  currency0 leg: {amt(f['in0'] - f['out0'], dec0)} net deposited, "
        f"{amt(held[0], dec0)} in the position now",
        f"  value of the position now, in currency0: {amt(now, dec0)}"
        + (" (both legs + the fees owed)" if fees is not None else " (both legs)"),
        f"  result against holding the deposit, at the price now, in currency0: "
        f"{amt(path, dec0)} ({parts}) against {amt(hold, dec0)} (the deposit held) "
        f"= {sign}{amt(abs(diff), dec0)}{pct}"
        + (" — the fees owed are counted" if fees is not None else " — the fees owed are NOT counted")
        + "; fees collected earlier are not. Not final until a withdrawal.",
    ]
    return lines


def depth_lines(st, dec0: Optional[int]) -> list:
    """The currency0 that moves the price 2% and 10%, the in-range liquidity
    held constant (a move that crosses a position's edge meets less)."""
    s = st.sqrt_price_x96 / M.Q96
    if s <= 0 or not st.liquidity:
        return ["  depth: no liquidity in range at this price"]
    x = st.liquidity / s
    paid = lambda move: int(x * ((1 - move) ** -0.5 - 1))
    taken = lambda move: int(x * (1 - (1 + move) ** -0.5))
    amt = V._raw_text
    return [f"  depth: {amt(paid(0.02), dec0)} currency0 paid in moves the price 2%, "
            f"{amt(paid(0.10), dec0)} moves it 10%; {amt(taken(0.02), dec0)} / "
            f"{amt(taken(0.10), dec0)} currency0 taken out the other way "
            f"(in-range liquidity held constant)"]


def holders(rpc, chain: str, pool_id: str, st) -> Dict[str, Any]:
    """The positions that hold liquidity in a pool, largest in-range first."""
    hist = pool_history(rpc, chain, pool_id)
    net: Dict[Tuple[str, int, int, int], int] = {}
    for c in hist.changes:
        key = (c.sender.lower(), c.tick_lower, c.tick_upper, c.salt)
        net[key] = net.get(key, 0) + c.delta
    live = {k: v for k, v in net.items() if v > 0}
    inside = {k: v for k, v in live.items() if k[1] <= st.tick < k[2]}
    if sum(inside.values()) != int(st.liquidity):
        raise LpReadError(f"the logs sum to in-range liquidity {sum(inside.values())}, "
                          f"the pool holds {st.liquidity}")
    return {"positions": len(live), "in_range": len(inside),
            "top": sorted(inside.items(), key=lambda kv: -kv[1])[:_TOP]}


def holder_lines(rpc, chain: str, pool_id: str, st) -> list:
    try:
        h = holders(rpc, chain, pool_id, st)
    except Exception as exc:
        return [f"  liquidity holders: NOT READ ({type(exc).__name__}: {str(exc)[:100]})"]
    row = dex_registry.row_for(chain, "v4")
    manager = str(row.position_manager or "").lower()
    parts = []
    for (sender, lo, hi, salt), liq in h["top"]:
        share = f"{liq / st.liquidity * 100:.1f}%"
        if sender != manager:
            parts.append(f"{share} [{lo},{hi}] held through {sender}")
            continue
        try:
            owner = str(view(rpc, row.position_manager, V.POSM_OWNER_OF, [salt])).lower()
        except Exception:
            owner = "owner not read"
        parts.append(f"{share} position #{salt} [{lo},{hi}] ({owner})")
    return [f"  liquidity holders: {h['positions']} position(s) hold liquidity, "
            f"{h['in_range']} in range at this price"
            + (" — largest in range: " + "; ".join(parts) if parts else "")]
