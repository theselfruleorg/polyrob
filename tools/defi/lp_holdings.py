"""The liquidity positions block of the holdings report.

Prod 2026-11-13: the owner asked for every balance and the answer left out a
position that held about half of the wallet's native coin. ``portfolio`` read
token balances only; a position is an NFT on a position manager, so the value
inside it was in no row and in no total, and nothing said so.

This block is part of the ONE holdings render (``data_holdings``). For each
pinned Uniswap deployment on the chain it reads how many positions the holder
owns (``balanceOf``, one call), finds their ids, reads each from the chain and
values both legs by the rule every total uses
(``tools.intel.price.counts_in_total``). The count is the proof: when fewer
positions are read than the chain says are held, the block says how many are
missing. It never prints "no positions" for a read that failed.

v3 ids come from the manager (it is enumerable). A v4 PositionManager is not:
ids come from its ``Transfer`` logs to the holder, newest window first, then
from the indexer; every candidate's owner is re-read on chain.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.wallet import chains, dex_registry, univ3_math as M
from tools.defi import lp_abi as A, lp_reads as R, lp_v4 as V
from tools.defi.data_render import _fmt_amount, _fmt_usd, _is_finite
from tools.intel.price import counts_in_total

logger = logging.getLogger("tools.defi.data_tool")

#: Positions read per protocol in one report; the rest are counted, not read.
MAX_READ = 12
#: The log scan: window size (a public node allows 10 M blocks per query) and
#: how many windows back from the head it looks.
LOG_WINDOW = 9_000_000
LOG_WINDOWS = 16

def _transfer_topic() -> str:
    from eth_utils import keccak
    return "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def v4_ids_from_logs(rpc: Callable, npm: str, holder: str, want: int,
                     *, deadline: Optional[float] = None) -> List[int]:
    """Ids of the position NFTs *holder* received from *npm*, newest first,
    from ``Transfer`` logs. Stops when *want* ids are found, the chain start is
    reached, the window budget is spent or *deadline* passes. Candidates only:
    the caller re-reads every owner. Raises on a failed log read."""
    head = int(rpc("eth_blockNumber", []), 16)
    to_topic = "0x" + "0" * 24 + holder.lower()[2:]
    found: List[int] = []
    hi = head
    for _ in range(LOG_WINDOWS):
        if hi < 0 or len(found) >= want:
            break
        if deadline is not None and time.monotonic() > deadline:
            break
        lo = max(0, hi - LOG_WINDOW)
        logs = rpc("eth_getLogs", [{"address": npm, "fromBlock": hex(lo), "toBlock": hex(hi),
                                    "topics": [_transfer_topic(), None, to_topic]}])
        for log in reversed(logs or []):
            topics = log.get("topics") or []
            if len(topics) == 4:
                token_id = int(str(topics[3]), 16)
                if token_id not in found:
                    found.append(token_id)
        hi = lo - 1
    return found


def _v4_ids(tool, rpc, chain: str, npm: str, holder: str, want: int,
            deadline: Optional[float]) -> List[int]:
    ids: List[int] = []
    try:
        ids = v4_ids_from_logs(rpc, npm, holder, want, deadline=deadline)
    except Exception:
        logger.debug("v4 position log scan failed on %s", chain, exc_info=True)
    if len(ids) < want:
        try:
            from tools.defi import nft_verbs
            held = nft_verbs.enumerate_holdings(chain=chain, address=holder,
                                                fetch=tool._nft_fetch, contracts=(npm,))
            for item in held:
                if (str(item.get("contract", "")).lower() == npm.lower()
                        and item.get("token_id") is not None):
                    token_id = int(str(item["token_id"]), 0)
                    if token_id not in ids:
                        ids.append(token_id)
        except Exception:
            logger.debug("v4 position indexer read failed on %s", chain, exc_info=True)
    return ids


def _leg(tool, chain: str, token: Optional[str], raw: int, decimals: Optional[int],
         native_symbol: str) -> Dict[str, Any]:
    """One leg of a position: amount, symbol, and its value by the total rule.
    ``token`` None = the native coin (priced through the wrapped native)."""
    row = chains.get(chain)
    address = token or getattr(row, "wrapped_native", None)
    symbol = native_symbol if token is None else tool._lp_symbol(chain, token)
    leg: Dict[str, Any] = {"token": token, "symbol": symbol, "raw": str(raw),
                           "amount": None, "usd": None, "counted": False}
    if decimals is None:
        return leg
    amount = raw / 10 ** decimals
    leg["amount"] = amount
    if not raw:
        leg.update(usd=0.0, counted=True)
        return leg
    try:
        info = tool._price_for(chain, address) if address else None
    except Exception:
        info = None
    price = getattr(info, "price_usd", None)
    if _is_finite(price) and price > 0:
        leg["usd"] = amount * price
        leg["counted"] = counts_in_total(getattr(info, "confidence", None))
    return leg


def _leg_text(leg: Dict[str, Any]) -> str:
    if leg["amount"] is None:
        return f"{leg['raw']} raw {leg['symbol']} (decimals unknown)"
    text = f"{_fmt_amount(leg['amount'])} {leg['symbol']}"
    if leg["usd"] is None:
        return text + " (no price)"
    if leg["counted"]:
        return text + f" = {_fmt_usd(leg['usd'])}"
    return text + f" (no reliable price, indicative ≈ {_fmt_usd(leg['usd'])})"


def _row(protocol: str, token_id: int, pool: str, fee: int, lo: int, hi: int,
         in_range: bool, legs: List[Dict[str, Any]], note: str = "") -> Dict[str, Any]:
    return {"protocol": protocol, "token_id": token_id, "pool": pool, "fee": fee,
            "tick_lower": lo, "tick_upper": hi, "in_range": in_range, "legs": legs,
            "note": note}


def _read_v3(tool, rpc, chain: str, token_id: int, native_symbol: str) -> Dict[str, Any]:
    pv = R.position(rpc, chain, token_id)
    legs = [_leg(tool, chain, token, int(amount) + int(fees), dec, native_symbol)
            for token, amount, fees, dec in ((pv.token0, pv.amount0, pv.fees0, pv.dec0),
                                             (pv.token1, pv.amount1, pv.fees1, pv.dec1))]
    return _row("v3", pv.token_id, pv.pool, pv.fee, pv.tick_lower, pv.tick_upper,
                pv.in_range, legs, "amounts include the fees owed")


def _read_v4(tool, rpc, chain: str, token_id: int, holder: str,
             native_symbol: str) -> Optional[Dict[str, Any]]:
    """None when the PositionManager names another owner (a stale candidate)."""
    pos = V.position(rpc, chain, token_id)
    if pos.owner != holder.lower():
        return None
    st = V.pool_state(rpc, chain, pos.pool_id)
    dec0, dec1 = V._decimals_pair(pos.key, lambda t: R.decimals(rpc, chain, t))
    if st.sqrt_price_x96 == 0:
        a0 = a1 = 0
        note = "the pool is NOT initialized"
    else:
        a0, a1 = M.amounts_for_liquidity(st.sqrt_price_x96, M.sqrt_price_at_tick(pos.tick_lower),
                                         M.sqrt_price_at_tick(pos.tick_upper), pos.liquidity)
        try:
            f0, f1 = V.position_fees(rpc, chain, pos)
            a0, a1 = a0 + f0, a1 + f1
            show = lambda n, d: f"{n} raw" if d is None else _fmt_amount(n / 10 ** d)
            note = (f"amounts include the fees owed: {show(f0, dec0)} + {show(f1, dec1)}")
        except Exception:
            note = "from the pool price; the fees owed were NOT READ and are not included"
    c0, c1 = pos.key["currency0"], pos.key["currency1"]
    legs = [_leg(tool, chain, None if c0 == V.ZERO else c0, a0, dec0, native_symbol),
            _leg(tool, chain, None if c1 == V.ZERO else c1, a1, dec1, native_symbol)]
    return _row("v4", pos.token_id, pos.pool_id, pos.key["fee"], pos.tick_lower, pos.tick_upper,
                pos.tick_lower <= st.tick < pos.tick_upper, legs, note)


def _protocol(tool, rpc, chain: str, holder: str, protocol: str, native_symbol: str,
              deadline: Optional[float]) -> Tuple[int, List[Dict[str, Any]], List[str]]:
    """``(held, rows, problems)`` for one deployment. Raises when the count
    itself cannot be read."""
    row = dex_registry.row_for(chain, protocol)
    npm = row.position_manager
    held = int(R.view(rpc, npm, A.NPM_BALANCE_OF, [holder]))
    if held == 0:
        return 0, [], []
    problems: List[str] = []
    if protocol == "v3":
        ids = [int(R.view(rpc, npm, A.NPM_TOKEN_OF_OWNER_BY_INDEX, [holder, i]))
               for i in range(min(held, MAX_READ))]
    else:
        ids = _v4_ids(tool, rpc, chain, npm, holder, held, deadline)
    rows: List[Dict[str, Any]] = []
    for token_id in ids:
        if len(rows) >= MAX_READ or (deadline is not None and time.monotonic() > deadline):
            break
        try:
            got = (_read_v3(tool, rpc, chain, token_id, native_symbol) if protocol == "v3"
                   else _read_v4(tool, rpc, chain, token_id, holder, native_symbol))
        except Exception as exc:
            problems.append(f"{protocol} position #{token_id}: NOT READ ({type(exc).__name__}: "
                            f"{str(exc)[:120]})")
            continue
        if got is not None:
            rows.append(got)
    missing = held - len(rows) - len(problems)
    if missing > 0:
        problems.append(
            f"{protocol}: the chain says {held} position(s) are held and {len(rows)} could be "
            f"read; {missing} more are NOT shown and their value is NOT in any total. Read one "
            f"by id: lp_positions(protocol='{protocol}', token_id=…)")
    return held, rows, problems


def build(tool, rpc: Callable, chain: str, holder: str,
          *, deadline: Optional[float] = None) -> Dict[str, Any]:
    """The block for *holder* on *chain*: ``{"lines", "counted_usd",
    "indicative_usd", "complete", "positions"}``. ``lines`` is empty only when
    the chain pins no Uniswap deployment. Never raises."""
    native_symbol = getattr(chains.get(chain), "native_symbol", None) or "native"
    rows: List[Dict[str, Any]] = []
    problems: List[str] = []
    read_any = False
    held_total = 0
    for protocol in ("v3", "v4"):
        row = dex_registry.row_for(chain, protocol)
        if row is None or not row.position_manager:
            continue
        read_any = True
        try:
            held, got, bad = _protocol(tool, rpc, chain, holder, protocol, native_symbol, deadline)
        except Exception as exc:
            problems.append(f"{protocol}: the position count was NOT READ ({type(exc).__name__}); "
                            f"this is not 'no positions'")
            continue
        held_total += held
        rows += got
        problems += bad
    out: Dict[str, Any] = {"lines": [], "counted_usd": 0.0, "indicative_usd": 0.0,
                           "complete": not problems, "positions": rows}
    if not read_any:
        return out
    if not rows and not problems:
        out["lines"] = ["liquidity positions: none (the pinned position managers were read on chain)"]
        return out
    lines = [f"liquidity positions ({len(rows)} read on chain; their value is inside the "
             f"position, NOT in the balances above):"]
    for r in rows:
        counted = sum(leg["usd"] for leg in r["legs"] if leg["counted"] and leg["usd"])
        loose = sum(leg["usd"] for leg in r["legs"] if not leg["counted"] and leg["usd"])
        out["counted_usd"] += counted
        out["indicative_usd"] += loose
        r["counted_usd"], r["indicative_usd"] = counted, loose
        lines.append(f"  {r['protocol']} #{r['token_id']}  {r['legs'][0]['symbol']}/"
                     f"{r['legs'][1]['symbol']}  fee {r['fee'] / 10000:g}%  ticks "
                     f"[{r['tick_lower']},{r['tick_upper']}] "
                     f"{'in range' if r['in_range'] else 'OUT of range'}")
        lines.append(f"    holds now: {_leg_text(r['legs'][0])} + {_leg_text(r['legs'][1])}"
                     + (f" ({r['note']})" if r["note"] else ""))
        lines.append(f"    pool: {r['pool']}")
    lines += [f"  ⚠️ {p}" for p in problems]
    out["lines"] = lines
    return out


def total_lines(block: Dict[str, Any], wallet_usd: Optional[float]) -> List[str]:
    """The lines under the wallet total: what the positions add, and the sum."""
    if not block.get("positions") and block.get("complete", True):
        return []
    lines = []
    counted, loose = block["counted_usd"], block["indicative_usd"]
    if block.get("positions"):
        lines.append(f"in liquidity positions: {_fmt_usd(counted)} (confidently valued legs)"
                     + (f"; NOT counted: indicative ≈ {_fmt_usd(loose)}" if loose else ""))
        if wallet_usd is not None:
            lines.append(f"wallet + liquidity positions: {_fmt_usd(wallet_usd + counted)}")
    if not block.get("complete", True):
        lines.append("the liquidity positions read is INCOMPLETE (see above): the sum may be higher")
    return lines
