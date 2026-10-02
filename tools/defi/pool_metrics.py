"""Pool metrics for a Pons v4 pool — the 090 §3 monitoring read (core handoff W9).

One deterministic read, no model step, safe for a cron read job
(``cron/read_job.py``): price and tick, in-range liquidity, the in-range
virtual reserves, the depth to move the token price by ±X%, the impact of one
mint buy, the 24 h change and the 30-min reference from this verb's OWN earlier
readings, and a code-hash check of every pinned v4 contract plus the pool's hook.

Every reading is recorded to the durable event log (kind ``pool_metrics``) —
090 §3 "each tick is a journal entry" — and the history figures are derived
from those rows only. A figure that cannot be derived says so; it is never 0.

⚠️ Depth and impact assume the CURRENT in-range liquidity stays constant over
the move (exact for a pool whose liquidity is one full-range position, as the
Pons graduation mints; a local figure when other ranges exist — a tick
crossing is not modelled). The mint-buy impact takes ``hook_fee_bps`` off the
input first (090: the hook collects 1%); that is an assumption from 048/090,
not read from the hook.
"""
import math
import statistics
import time
from typing import Any, Callable, Dict, List, Optional

from core.wallet import dex_registry
from tools.defi import lp_reads, lp_v4 as V

EVENT_KIND = "pool_metrics"
_DAY = 86400.0
_DAY_TOLERANCE = 3600.0     # a reading within ±1 h of 24 h ago counts as "24 h ago"
_REFERENCE_TICKS = 15       # 090 §3: median of the last 15 ticks


def depth_for_move(sqrt_price_x96: int, liquidity: int, pct: float):
    """``(currency0_in, currency1_out, currency1_in, currency0_out)`` in RAW
    units to move the currency1 price (in currency0) up by ``pct`` % (currency0
    in) and down by ``pct`` % (currency1 in), at constant liquidity."""
    s = sqrt_price_x96 / 2 ** 96
    x = pct / 100.0
    up = s / math.sqrt(1 + x)            # token price +x  => p' = p / (1 + x)
    c0_in = liquidity * (1 / up - 1 / s)
    c1_out = liquidity * (s - up)
    if x < 1:
        down = s / math.sqrt(1 - x)      # token price -x  => p' = p / (1 - x)
        c1_in = liquidity * (down - s)
        c0_out = liquidity * (1 / s - 1 / down)
    else:
        c1_in = c0_out = float("inf")
    return c0_in, c1_out, c1_in, c0_out


def buy_impact_pct(sqrt_price_x96: int, liquidity: int, amount0_raw: float,
                   fee_bps: int) -> float:
    """% rise of the currency1 price after ``amount0_raw`` of currency0 buys
    currency1, at constant liquidity, with ``fee_bps`` taken from the input."""
    if liquidity <= 0:
        return float("inf")
    s = sqrt_price_x96 / 2 ** 96
    dx = amount0_raw * (10000 - fee_bps) / 10000
    s_after = 1 / (1 / s + dx / liquidity)
    return ((s / s_after) ** 2 - 1) * 100.0


def _history(chain: str, pool_id: str, now: float, *, query_fn=None) -> Optional[List[Dict[str, Any]]]:
    """This verb's earlier readings of this pool, most recent first. None when
    the event log is off or unreadable (unknown, not empty)."""
    try:
        if query_fn is None:
            from core.event_log import event_log_enabled, get_event_log
            if not event_log_enabled():
                return None
            query_fn = get_event_log().query
        rows = query_fn(since_ts=now - _DAY - _DAY_TOLERANCE, kind=EVENT_KIND, limit=2000)
    except Exception:
        return None
    return [r for r in rows if (r.get("attrs") or {}).get("pool_id") == pool_id
            and (r.get("attrs") or {}).get("chain") == chain
            and isinstance((r.get("attrs") or {}).get("price"), (int, float))]


def _record(attrs: Dict[str, Any], *, record_fn=None) -> None:
    try:
        if record_fn is None:
            from core.event_log import event_log_enabled, get_event_log
            if not event_log_enabled():
                return
            record_fn = get_event_log().record
        record_fn(EVENT_KIND, source="defi_data.pool_metrics", attrs=attrs)
    except Exception:
        pass


def pool_metrics(rpc: Callable, chain: str, token: str, *, expect_pool_id: Optional[str] = None,
                 depth_pct=(1.0, 2.0, 5.0), mint_native: float = 0.021,
                 hook_fee_bps: int = 100, now: Optional[float] = None,
                 query_fn=None, record_fn=None) -> Dict[str, Any]:
    """The metrics as a dict: ``lines`` (text, ALERT lines first), ``alerts``,
    and the figures. Raises ``LpReadError`` when the pool itself is unreadable."""
    now = time.time() if now is None else now
    alerts: List[str] = []
    key = V.pons_key_for(rpc, token)
    pid = lp_reads.pool_id(key)
    if expect_pool_id and expect_pool_id.lower() != pid.lower():
        alerts.append(f"ALERT: the Pons PoolKey for {token} hashes to {pid}, not the "
                      f"expected {expect_pool_id} (090 R0.1/R6: stop writes)")
    try:
        dex_registry.verify_pins(rpc, chain, "v4")
        dex_registry.verify_hook(rpc, chain, key["hooks"])
        pins = "OK (PoolManager, PositionManager, StateView, Permit2, hook)"
    except dex_registry.DexPinError as exc:
        pins = f"CHANGED: {exc}"
        alerts.append(f"ALERT: code hash check failed — {exc} (090 R0.4/R6: stop writes)")
    st = V.pool_state(rpc, chain, pid)
    if st.sqrt_price_x96 == 0:
        raise lp_reads.LpReadError(f"v4 pool {pid} is not initialized")
    native0 = key["currency0"] == V.ZERO
    dec0 = 18 if native0 else lp_reads.decimals(rpc, chain, key["currency0"])
    dec1 = lp_reads.decimals(rpc, chain, key["currency1"])
    s = st.sqrt_price_x96 / 2 ** 96
    token_per_native = s * s * 10 ** dec0 / 10 ** dec1        # currency1 per currency0
    price = 1 / token_per_native                              # currency0 per currency1
    x_raw, y_raw = V.reserves_full_range(st.sqrt_price_x96, st.liquidity)
    reserve0, reserve1 = x_raw / 10 ** dec0, y_raw / 10 ** dec1
    impact = buy_impact_pct(st.sqrt_price_x96, st.liquidity, mint_native * 10 ** dec0, hook_fee_bps)
    depth = {}
    for pct in depth_pct:
        c0_in, c1_out, c1_in, c0_out = depth_for_move(st.sqrt_price_x96, st.liquidity, float(pct))
        depth[float(pct)] = {"up_currency0_in": c0_in / 10 ** dec0,
                             "down_currency1_in": c1_in / 10 ** dec1}

    hist = _history(chain, pid, now, query_fn=query_fn)
    change_24h = reference = None
    if hist is None:
        change_note = "24h change: not derivable (event log off or unreadable)"
        ref_note = "30-min reference: not derivable (event log off or unreadable)"
    else:
        target = now - _DAY
        near = [r for r in hist if abs(float(r["ts"]) - target) <= _DAY_TOLERANCE]
        if near:
            old = min(near, key=lambda r: abs(float(r["ts"]) - target))
            change_24h = (price / float(old["attrs"]["price"]) - 1) * 100.0
            change_note = (f"24h change: {change_24h:+.2f}% (vs a reading "
                           f"{(now - float(old['ts'])) / 3600:.1f} h ago)")
        else:
            change_note = "24h change: not derivable (no reading of this pool 23-25 h ago)"
        recent = [float(r["attrs"]["price"]) for r in hist[:_REFERENCE_TICKS]]
        if recent:
            reference = statistics.median(recent)
            ref_note = (f"reference (median of last {len(recent)} readings): {reference:.6g}; "
                        f"now {(price / reference - 1) * 100:+.2f}% from it")
        else:
            ref_note = "reference: not derivable (no earlier reading of this pool)"

    c0 = "native" if native0 else key["currency0"]
    lines = list(alerts) + [
        f"pool_metrics {chain} v4 pool {pid} ({c0}/{key['currency1']}, hooks {key['hooks']})",
        f"  price: 1 token = {price:.6g} {c0}   |   1 {c0} = {token_per_native:.6g} token",
        f"  tick: {st.tick}   sqrtPriceX96: {st.sqrt_price_x96}   lpFee: {st.lp_fee}",
        f"  liquidity (in range): {st.liquidity:,}",
        f"  in-range reserves: {reserve0:.6g} {c0} + {reserve1:.6g} token",
        f"  one mint buy ({mint_native:g} {c0}, {hook_fee_bps} bps to the hook): "
        f"+{impact:.3f}% token price",
    ] + [
        f"  depth ±{pct:g}%: {d['up_currency0_in']:.6g} {c0} in to lift the token "
        f"price {pct:g}%; {d['down_currency1_in']:.6g} token in to drop it {pct:g}%"
        for pct, d in depth.items()
    ] + [f"  {change_note}", f"  {ref_note}", f"  code hashes: {pins}",
         f"  read via: {st.source}; depth/impact assume constant in-range liquidity"]
    out = {"lines": lines, "alerts": alerts, "chain": chain, "pool_id": pid,
           "price": price, "token_per_native": token_per_native, "tick": st.tick,
           "sqrt_price_x96": str(st.sqrt_price_x96), "liquidity": str(st.liquidity),
           "reserve0": reserve0, "reserve1": reserve1, "mint_impact_pct": impact,
           "change_24h_pct": change_24h, "reference_price": reference,
           "pins_ok": not pins.startswith("CHANGED")}
    _record({k: v for k, v in out.items() if k != "lines"}, record_fn=record_fn)
    return out
