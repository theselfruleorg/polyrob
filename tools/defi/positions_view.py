"""``defi_data.positions`` — the rail book, with the arithmetic done (071 W3).

The model used to compute unrealized and realized P&L, barrier percentages and a
trailing high-water mark by hand (``position-journal``, ``exits``,
``sizing-and-risk``). That is the class of error behind the $7-vs-$6,780 report:
a figure the model converted itself. This module does every one of those sums
from the rail-written store (``core/open_positions.py``) and the current price,
and the skills tell the model to REPEAT these figures, never to compute one.

Rules it keeps:
- ONE cost method, average cost, matching the store's own reductions.
- UNKNOWN is not zero: a NULL basis is "basis unknown" and yields no P&L; a
  missing price yields no value; a realized figure with an unknown leg says so.
- The high-water mark is only raised from a HIGH-confidence price — a thin pool
  can print any number, and a fake peak would trip a trailing stop.

Pure rendering over injected reads, so the unit tests never reach the network.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Callable, Dict, List, Optional, Tuple

QTY_SOURCE_WORDS = {
    "receipt": "legacy Transfer events, unverified quantity",
    "simulation": "SIMULATED balance change, final fill not measured",
    "quote": "QUOTED size, not measured",
    "inherited": "held when the account was adopted",
    "": "size source not recorded (row written before 071)",
}

#: 2026-10-14 review C-7: a position worth less than this is DUST. An exit run
#: spent 1.14M tokens trying to sell $0.005 (gas costs more) and messaged the
#: owner about it; the exits skill skips dust and never escalates it.
DUST_USD = 1.0


def is_dust(value_usd: Optional[float]) -> bool:
    """A KNOWN value under :data:`DUST_USD`. An unknown value is never dust."""
    return value_usd is not None and value_usd < DUST_USD


STATUS_WORDS = {"open": "open", "quarantined": "QUARANTINED look-alike",
                "written_off": "WRITTEN OFF by the owner"}


@dataclass
class PositionFigures:
    chain: str
    address: str
    symbol: str
    status: str
    qty: float
    qty_source: str
    basis_usd: Optional[float]
    basis_per_token_usd: Optional[float]
    price_usd: Optional[float]
    price_confidence: Optional[str]
    value_usd: Optional[float]
    unrealized_usd: Optional[float]
    unrealized_pct: Optional[float]
    realized_usd: Optional[float]
    realized_known_legs: int
    realized_unknown_legs: int
    high_water_usd: Optional[float]
    high_water_ts: Optional[float]
    from_high_pct: Optional[float]
    entry_ts: Optional[float]


def _finite(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _usd(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    a = abs(v)
    if a == 0:
        return "$0.00"
    if a >= 1:
        return f"${v:,.2f}"
    # sub-dollar: keep 4 significant figures (a memecoin price is not $0.00)
    return f"${v:.4g}"


def _signed_usd(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    return ("+" if v >= 0 else "-") + _usd(abs(v))


def _pct(v: Optional[float]) -> str:
    return "unknown" if v is None else f"{v:+.1f}%"


def _qty(v: float) -> str:
    return f"{v:,.6f}".rstrip("0").rstrip(".") if v else "0"


def figures(entry, price_info, realized=None,
            high_water: Optional[float] = None) -> PositionFigures:
    """The per-position sums. ``entry`` is a ``core.open_positions.PositionEntry``;
    ``price_info`` anything with ``price_usd``/``confidence`` (or None);
    ``realized`` a ``RealizedEntry`` (or None)."""
    qty = float(entry.qty)
    basis = _finite(entry.entry_usd) if entry.entry_usd is not None else None
    price = _finite(getattr(price_info, "price_usd", None)) if price_info else None
    if price is not None and price <= 0:
        price = None
    conf = getattr(price_info, "confidence", None) if price_info else None
    if conf == "disputed":
        # 071 review: the sources disagree beyond tolerance — no value, P&L or
        # distance-from-high is computed on it (an exit rule must not fire on a
        # number the sources do not agree on). LOW prices remain indicative
        # values, but cannot produce stop/target/trailing barrier figures.
        price = None
    value = qty * price if price is not None else None
    unreal = unreal_pct = None
    if value is not None and basis is not None and conf == "high":
        unreal = value - basis
        unreal_pct = (unreal / basis * 100.0) if basis > 0 else None
    hw = high_water if high_water is not None else entry.high_water_usd
    from_high = ((price - hw) / hw * 100.0
                 if (price is not None and hw is not None and hw > 0 and conf == "high") else None)
    r_known = int(getattr(realized, "known_legs", 0) or 0)
    r_unknown = int(getattr(realized, "unknown_legs", 0) or 0)
    r_usd = (float(realized.realized_usd) if realized is not None and r_known else
             (None if r_unknown else 0.0))
    return PositionFigures(
        chain=entry.chain, address=entry.address, symbol=str(entry.symbol or "?"),
        status=entry.status or "open", qty=qty, qty_source=entry.qty_source or "",
        basis_usd=basis,
        basis_per_token_usd=(basis / qty if basis is not None and qty > 0 else None),
        price_usd=price, price_confidence=conf, value_usd=value,
        unrealized_usd=unreal, unrealized_pct=unreal_pct,
        realized_usd=r_usd, realized_known_legs=r_known, realized_unknown_legs=r_unknown,
        high_water_usd=hw, high_water_ts=entry.high_water_ts, from_high_pct=from_high,
        entry_ts=entry.entry_ts)


def _realized_words(usd: Optional[float], known: int, unknown: int) -> str:
    if not known and not unknown:
        return "realized $0.00 (no sell booked)"
    if not known:
        return f"realized unknown ({unknown} sell(s) with an unknown basis or proceeds)"
    words = f"realized {_signed_usd(usd)} ({known} sell(s), average cost)"
    if unknown:
        words += (f" — PARTIAL: {unknown} more sell(s) had an unknown basis or "
                  f"proceeds and are NOT in this figure")
    return words


def _day(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def render(rows: List[PositionFigures], closed: List[dict], *,
           chain: Optional[str]) -> str:
    scope = f"chain {chain}" if chain else "every chain"
    out = [f"OPEN POSITIONS — the money rail's book (open_positions), {scope}.",
           "Cost method: average cost. Every figure below is computed here — "
           "repeat it as written; never compute, convert or re-derive a money "
           "figure yourself. 'unknown' is not zero."]
    if not rows:
        out += ["", "No open position is recorded in the rail book"
                + (f" on {chain}." if chain else ".")]
    by_chain: Dict[str, List[PositionFigures]] = {}
    for r in rows:
        by_chain.setdefault(r.chain, []).append(r)
    for c in sorted(by_chain):
        out += ["", f"{c}:"]
        for r in by_chain[c]:
            head = f"  {r.symbol} {r.address} — qty {_qty(r.qty)} " \
                   f"({QTY_SOURCE_WORDS.get(r.qty_source, r.qty_source)})"
            if r.status != "open":
                head += f" · {STATUS_WORDS.get(r.status, r.status)}"
            out.append(head)
            basis = ("basis unknown" if r.basis_usd is None
                     else f"basis {_usd(r.basis_usd)} ({_usd(r.basis_per_token_usd)} per token)")
            price = ("price unknown (no indexed pool)" if r.price_usd is None
                     else f"price {_usd(r.price_usd)} per token (confidence "
                          f"{r.price_confidence or 'unknown'})")
            out.append(f"    {basis} · {price} · value {_usd(r.value_usd)}")
            if is_dust(r.value_usd):
                out.append(f"    DUST: worth under {_usd(DUST_USD)} — below the exit floor. "
                           "Do not sell it (gas costs more) and do not report it to the owner.")
            if r.unrealized_usd is not None:
                unreal = f"unrealized {_signed_usd(r.unrealized_usd)} ({_pct(r.unrealized_pct)})"
            elif r.basis_usd is None:
                unreal = "unrealized unknown (basis unknown)"
            elif r.price_usd is not None and r.price_confidence != "high":
                unreal = "unrealized unknown (price is not corroborated)"
            else:
                unreal = "unrealized unknown (no price)"
            line = (f"    {unreal} · "
                    f"{_realized_words(r.realized_usd, r.realized_known_legs, r.realized_unknown_legs)}")
            out.append(line)
            if r.high_water_usd is not None:
                out.append(f"    high-water {_usd(r.high_water_usd)} per token "
                           f"(highest high-confidence price a positions read saw, "
                           f"{_day(r.high_water_ts)}) · from high {_pct(r.from_high_pct)}")
            else:
                out.append("    high-water not observed yet")
            if r.price_confidence == "low":
                out.append("    ⚠ low-confidence price: thin liquidity — the value "
                           "is indicative only; stop, target and trailing figures are unavailable. "
                           "Obtain an independent executable exit quote before a price barrier can fire.")
            elif r.price_confidence == "disputed":
                out.append("    ⚠ price DISPUTED: the sources disagree too much — no "
                           "value or P&L is computed until they agree")
    priced = [r for r in rows if r.value_usd is not None]
    if rows:
        total = sum(r.value_usd for r in priced)
        known_basis = [r for r in rows if r.unrealized_usd is not None]
        out += ["", f"total value {_usd(total)} over {len(priced)} priced position(s)"
                + (f"; {len(rows) - len(priced)} unpriced, NOT included"
                   if len(priced) < len(rows) else "")]
        dust = [r for r in rows if is_dust(r.value_usd)]
        if dust:
            out.append(f"{len(dust)} DUST position(s) under {_usd(DUST_USD)}: "
                       + ", ".join(r.symbol for r in dust) + " — skip them on an exit check")
        if known_basis:
            out.append(f"total unrealized {_signed_usd(sum(r.unrealized_usd for r in known_basis))}"
                       f" over {len(known_basis)} position(s) with a known basis and price"
                       + (f"; {len(rows) - len(known_basis)} NOT included"
                          if len(known_basis) < len(rows) else ""))
    if closed:
        out += ["", "closed (realized only):"]
        for c in closed:
            out.append(f"  {c['symbol'] or '?'} {c['address']} on {c['chain']} — "
                       + _realized_words(c["realized_usd"], c["known_legs"],
                                         c["unknown_legs"]))
    return "\n".join(out)


def build(entries, realized, price_fn: Callable[[str, str], object], *,
          chain: Optional[str],
          observe_fn: Optional[Callable[[object, float], Optional[float]]] = None,
          ) -> Tuple[str, dict]:
    """Price every position, do the sums, render. Returns ``(text, metadata)``.

    ``observe_fn(entry, price)`` raises the stored high-water mark and returns
    it; it is only called with a HIGH-confidence price."""
    rmap = {(r.chain, r.address): r for r in realized or []}
    rows: List[PositionFigures] = []
    for e in entries or []:
        info = None
        try:
            info = price_fn(e.chain, e.address)
        except Exception:
            info = None
        hw = None
        price = _finite(getattr(info, "price_usd", None)) if info is not None else None
        if (observe_fn is not None and price is not None and price > 0
                and getattr(info, "confidence", None) == "high"
                and (e.status or "open") == "open"):
            try:
                hw = observe_fn(e, price)
            except Exception:
                hw = None
        rows.append(figures(e, info, rmap.get((e.chain, e.address)), high_water=hw))
    open_keys = {(e.chain, e.address) for e in entries or []}
    closed = [dict(chain=r.chain, address=r.address, symbol=r.symbol,
                   realized_usd=(r.realized_usd if r.known_legs else None),
                   known_legs=r.known_legs, unknown_legs=r.unknown_legs,
                   sold_qty=r.sold_qty)
              for r in realized or [] if (r.chain, r.address) not in open_keys]
    text = render(rows, closed, chain=chain)
    meta = {"positions": [{**asdict(r), "dust": is_dust(r.value_usd)} for r in rows],
            "closed": closed, "cost_method": "average_cost", "dust_usd": DUST_USD}
    return text, meta
