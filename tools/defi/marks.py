"""``defi_data.position_mark`` + ``defi_data.discovery_count`` — the mandate jobs'
$0 pre-check reads (086 W3, §3.2/§3.3).

Mixed into :class:`tools.defi.data_tool.DefiDataTool`; both are ``read_job`` rows
of the verb-policy table, so a cron ``precheck`` runs them in-process with no
model turn.

``position_mark(chain, account, address)`` reads ONE book row and the token's
price and returns typed metadata::

    {qty, cost_usd, value_usd, unrealized_pct, from_high_pct, high_water_usd,
     hours_open, dust, closed, status, price_usd, price_grade,
     barrier, barrier_reason, exit_preset, mandate}

* ``closed`` — the book holds no ACTIVE row (none, written off, retired). ``dust``
  — the book row's status is ``dust``. A precheck ``cancel_when`` reads these two.
* ``price_grade`` — the quote's confidence: ``high`` is spend grade; ``low``,
  ``disputed`` or ``unknown`` are not. ``unrealized_pct`` / ``from_high_pct`` are
  set ONLY on a ``high`` price (``positions_view.figures``, one rule).
* ``barrier`` — ``none`` · ``near`` · ``crossed`` · ``unknown``, computed here from
  the mandate's exit preset (``payload.mandate`` -> ``Mandate.effective_preset().exit``;
  the runner injects the job's mandate id), so the numbers stay in
  ``core/trading_presets.py`` and never in a job's text. A barrier is only
  ``crossed`` on a spend-grade price (or by the clock: the time stop). A crossing
  seen on a weaker price, an unreadable price and an unknown basis are
  ``unknown`` — the precheck WAKES and the model run decides; nothing fires.
  An exit job's precheck alerts on ``{"field": "barrier", "not_equals": "none"}``.

⚠️ Side effect, on purpose: like ``defi_data.positions``, a mark raises the row's
stored high-water mark (``core.open_positions.observe_price``) — only from a
``high`` price on an ``open`` row. The trailing stop needs the peak between
model runs, and the $0 tick is the only reader that sees it.

Take-profit tiers: a partial sell booked since the row opened spends ONE tier (the
two presets have one tier each). A preset with several tiers needs a per-row tier
count; until then a second tier reads as the first one spent.

``discovery_count(chains, ...)`` wraps ``tools.defi.discovery.discovery_candidates``
and, when the job belongs to a mandate, the hunt readiness
(``core.money.mandate_gate.hunt_refusal`` — the entry step's own rules)::

    {count, unavailable_chains, mandate_ready, mandate_reason, actionable}

``actionable`` is what a hunt precheck alerts on (``above: 0``): the count when the
mandate may take an entry, ``0`` when it may not (paused, slots full, allocation
used — a quiet tick), ``None`` (wakes) when the mandate or the book could not be
read, or when every discovery read failed.
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import asyncio
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

NONE, NEAR, CROSSED, UNKNOWN = "none", "near", "crossed", "unknown"
_RANK = {NONE: 0, NEAR: 1, UNKNOWN: 2, CROSSED: 3}


class PositionMarkParams(BaseModel):
    chain: str = Field(description="The chain of the book row (base, robinhood, solana, …)")
    account: str = Field(description="The holder of the book row: '' = the treasury wallet, "
                                     "else the token-bound account address")
    address: str = Field(description="The token contract / mint address of the book row")
    mandate: Optional[str] = Field(
        default=None, description="The trading mandate id whose exit preset sets the "
                                  "barriers. A scheduled job's own payload.mandate wins.")


class DiscoveryCountParams(BaseModel):
    chains: List[str] = Field(description="The chains to read new / trending pools on")
    kinds: Optional[List[str]] = Field(default=None, description="'new' and/or 'trending'")
    max_age_h: Optional[float] = Field(default=None, gt=0)
    min_age_h: Optional[float] = Field(default=None, ge=0)
    min_liquidity_usd: Optional[float] = Field(default=None, ge=0)
    min_volume_h24_usd: Optional[float] = Field(default=None, ge=0)
    mandate: Optional[str] = Field(
        default=None, description="The trading mandate id the hunt belongs to. A "
                                  "scheduled job's own payload.mandate wins.")


def _finite(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _gain(price: Optional[float], per_token: Optional[float]) -> Optional[float]:
    if price is None or per_token is None or per_token <= 0:
        return None
    # rounded: a price exactly at a barrier must read AT it, not 1e-14 short.
    return round((price / per_token - 1.0) * 100.0, 9)


def barrier_for(exit_preset, *, price: Optional[float], grade: str,
                basis_per_token: Optional[float], high_water: Optional[float],
                hours_open: Optional[float], tiers_taken: int = 0) -> Tuple[str, str]:
    """``(barrier, reason)`` for one position. PURE.

    ``reason`` names every barrier at the winning state, comma-joined
    (``stop_loss`` · ``take_profit`` · ``trailing`` · ``time_stop`` ·
    ``price_unreadable`` · ``basis_unknown`` · ``<barrier>_unverified``)."""
    if exit_preset is None:
        return NONE, "no_exit_preset"
    e = exit_preset
    found: List[Tuple[str, str]] = []
    peak_gain = _gain(high_water, basis_per_token)
    if e.time_stop_h is not None and hours_open is not None and hours_open >= e.time_stop_h:
        reached = (e.time_stop_min_gain_pct is not None and peak_gain is not None
                   and peak_gain >= e.time_stop_min_gain_pct)
        if not reached:
            found.append((CROSSED, "time_stop"))
    gain = _gain(price, basis_per_token)
    if price is None:
        found.append((UNKNOWN, "price_unreadable"))
    elif basis_per_token is None:
        found.append((UNKNOWN, "basis_unknown"))
    else:
        near = float(e.near_pct)
        price_marks: List[Tuple[str, str]] = []
        if gain <= e.stop_pct:
            price_marks.append((CROSSED, "stop_loss"))
        elif gain <= e.stop_pct + near:
            price_marks.append((NEAR, "stop_loss"))
        tiers = list(e.take_profit or ())
        if tiers_taken < len(tiers):
            at = float(tiers[tiers_taken][0])
            if gain >= at:
                price_marks.append((CROSSED, "take_profit"))
            elif gain >= at - near:
                price_marks.append((NEAR, "take_profit"))
        if e.trail_pct is not None:
            top = max(x for x in (peak_gain, gain) if x is not None)
            hw = max(high_water or 0.0, price)
            if top >= float(e.trail_arm_pct) and hw > 0:
                from_high = round((price - hw) / hw * 100.0, 9)
                if from_high <= -float(e.trail_pct):
                    price_marks.append((CROSSED, "trailing"))
                elif from_high <= -(float(e.trail_pct) - near):
                    price_marks.append((NEAR, "trailing"))
        for state, why in price_marks:
            if state == CROSSED and grade != "high":
                found.append((UNKNOWN, f"{why}_unverified"))
            else:
                found.append((state, why))
    if not found:
        return NONE, ""
    best = max(_RANK[s] for s, _ in found)
    state = next(s for s, r in _RANK.items() if r == best)
    return state, ",".join(dict.fromkeys(w for s, w in found if s == state))


def _exit_preset_for(mandate_id: Optional[str], user_id: str):
    """``(exit_preset, preset_name, error)``; no mandate = no preset, no error."""
    if not mandate_id:
        return None, "", ""
    from core.trading_mandate import MandateUnreadable, get
    try:
        m = get(mandate_id)
    except MandateUnreadable as exc:
        return None, "", f"the trading mandate could not be read ({exc})"
    if m is None or str(m.user_id) != str(user_id):
        return None, "", f"trading mandate {mandate_id} does not exist for this tenant"
    try:
        p = m.effective_preset()
    except Exception as exc:
        return None, "", f"trading mandate {m.id} has invalid numbers ({exc})"
    return p.exit, (p.exit.name if p.exit else ""), ""


class MarksReadsMixin:
    """The two $0 reads a mandate's cron prechecks run."""

    @BaseTool.action(
        "Mark ONE position of this agent's own book: size, cost, value, unrealized "
        "% and distance from the high (on a spend-grade price only), hours open, and "
        "whether its exit barriers (from the trading mandate's preset) are near or "
        "crossed. Built for an exit job's $0 precheck. Reveals own funds — owner-only.",
        param_model=PositionMarkParams)
    async def position_mark(self, params: PositionMarkParams, execution_context=None):
        return await asyncio.to_thread(self._position_mark_sync, params, execution_context)

    def _position_mark_sync(self, params: PositionMarkParams, execution_context=None):
        from tools.defi.data_tool import _operator_read_refusal  # call-time
        refusal = _operator_read_refusal(execution_context)
        if refusal:
            return self._ar(error=refusal)
        uid = self._rail_uid(execution_context)
        if not uid:
            return self._ar(error="no tenant on this call — cannot pick whose book to read")
        chain = str(params.chain or "").strip().lower()
        from core import open_positions as op
        acct = op._account_key(params.account)
        try:
            # STRICT reads: an unreadable book raises (a wake), never "closed".
            want = op._norm(params.address)
            pos = next((p for p in op.positions_for(uid, chain=chain, account=acct,
                                                    statuses=op.STATUSES)
                        if op._norm(p.address) == want), None)
            realized = [r for r in op.realized_for(uid, chain=chain, account=acct)
                        if op._norm(r.address) == want]
        except Exception as exc:
            return self._ar(error=(f"the rail book could not be read ({type(exc).__name__}) "
                                   f"— this is NOT a closed position"))
        exit_preset, preset_name, perr = _exit_preset_for(params.mandate, uid)
        meta: Dict[str, Any] = {"chain": chain, "account": acct, "address": params.address,
                                "mandate": params.mandate or None,
                                "exit_preset": preset_name or None}
        if pos is None or pos.status not in op.ACTIVE_STATUSES:
            dust = pos is not None and pos.status == op.STATUS_DUST
            meta.update(qty=(pos.qty if pos else 0.0), cost_usd=(pos.entry_usd if pos else None),
                        value_usd=None, unrealized_pct=None, from_high_pct=None,
                        high_water_usd=(pos.high_water_usd if pos else None),
                        hours_open=None, dust=dust, closed=not dust,
                        status=(pos.status if pos else "none"), price_usd=None,
                        price_grade="unknown", barrier=NONE, barrier_reason="closed")
            what = (f"DUST ({pos.status_reason or 'marked dust'})" if dust else
                    f"CLOSED (book status: {pos.status if pos else 'no row'})")
            return self._ar(content=f"{params.address} on {chain}: {what}.", metadata=meta)
        try:
            info = self._price_for(chain, pos.address)
        except Exception:
            info = None
        grade = str(getattr(info, "confidence", None) or "unknown") if info else "unknown"
        price = _finite(getattr(info, "price_usd", None)) if info else None
        if price is not None and price <= 0:
            price = None
        if grade == "disputed":
            price = None
        if price is None:
            grade = "unknown" if grade != "disputed" else grade
        hw = None
        if price is not None and grade == "high" and pos.status == op.STATUS_OPEN:
            hw = op.observe_price(uid, chain, pos.address, price, account=acct)
        from tools.defi import positions_view
        fig = positions_view.figures(pos, info, None, high_water=hw)
        now = time.time()
        hours = max(0.0, (now - float(pos.entry_ts)) / 3600.0) if pos.entry_ts else None
        taken = 1 if any(r.sold_qty > 0 and float(r.updated_ts) >= float(pos.entry_ts)
                         for r in realized) else 0
        if perr:
            barrier, why = UNKNOWN, "mandate_unreadable"
        else:
            barrier, why = barrier_for(
                exit_preset, price=price, grade=grade, basis_per_token=fig.basis_per_token_usd,
                high_water=fig.high_water_usd, hours_open=hours, tiers_taken=taken)
        value = fig.value_usd
        meta.update(qty=pos.qty, cost_usd=pos.entry_usd, value_usd=value,
                    unrealized_pct=fig.unrealized_pct, from_high_pct=fig.from_high_pct,
                    high_water_usd=fig.high_water_usd,
                    hours_open=None if hours is None else round(hours, 2),
                    dust=False, closed=False, status=pos.status, price_usd=price,
                    price_grade=grade, barrier=barrier, barrier_reason=why,
                    tiers_taken=taken)
        lines = [f"{pos.symbol or params.address} on {chain}: qty {pos.qty:,.6g}, cost "
                 f"{positions_view._usd(pos.entry_usd)}, value {positions_view._usd(value)}, "
                 f"unrealized {positions_view._pct(fig.unrealized_pct)}, from high "
                 f"{positions_view._pct(fig.from_high_pct)}, price grade {grade}, open "
                 f"{'unknown' if hours is None else f'{hours:.1f} h'}.",
                 f"Exit barrier ({preset_name or 'no preset'}): {barrier}"
                 + (f" — {why}" if why else "") + "."]
        if perr:
            lines.append(f"The barriers are UNKNOWN: {perr}.")
        return self._ar(content="\n".join(lines), metadata=meta)

    @BaseTool.action(
        "Count the new / trending pools on the given chains that pass the hunt's cheap "
        "pre-screen (age, liquidity, 24h volume, market verdict; no LLM). With a trading "
        "mandate it also says whether the mandate may take an entry now. Built for a "
        "hunt job's $0 precheck.", param_model=DiscoveryCountParams)
    async def discovery_count(self, params: DiscoveryCountParams, execution_context=None):
        return await asyncio.to_thread(self._discovery_count_sync, params, execution_context)

    def _discovery_count_sync(self, params: DiscoveryCountParams, execution_context=None):
        ready: Optional[bool] = True
        reason = ""
        held: Tuple[Tuple[str, str], ...] = ()
        chains = [str(c or "").strip().lower() for c in params.chains if str(c or "").strip()]
        if params.mandate:
            uid = self._rail_uid(execution_context)
            from core.money.mandate_gate import hunt_refusal
            r = hunt_refusal(params.mandate, user_id=uid)
            ready, reason, held = r.ready, r.reason, r.held
            if r.chains:
                chains = [c for c in chains if c in r.chains] or list(r.chains)
        meta: Dict[str, Any] = {"chains": chains, "mandate": params.mandate or None,
                                "mandate_ready": ready, "mandate_reason": reason}
        if ready is False:
            # Entries are refused anyway: no read, a quiet $0 tick.
            meta.update(count=None, unavailable_chains=[], actionable=0, candidates=[])
            return self._ar(content=f"Hunt not ready: {reason}. Discovery not read.",
                            metadata=meta)
        from tools.defi import discovery
        kw: Dict[str, Any] = {k: getattr(params, k) for k in
                              ("max_age_h", "min_age_h", "min_liquidity_usd",
                               "min_volume_h24_usd") if getattr(params, k) is not None}
        if params.kinds:
            kw["kinds"] = tuple(params.kinds)
        out = discovery.discovery_candidates(chains, exclude_tokens=held,
                                             discover_fn=self._discover,
                                             **kw)
        all_down = bool(chains) and len(out["unavailable_chains"]) >= len(chains)
        actionable = None if (ready is None or all_down) else out["count"]
        meta.update(count=out["count"], unavailable_chains=out["unavailable_chains"],
                    complete=out["complete"], actionable=actionable,
                    candidates=out["candidates"][:10], rejected=out["rejected"])
        text = (f"{out['count']} candidate(s) on {', '.join(chains) or 'no chain'}"
                + (f"; UNAVAILABLE (not empty): {', '.join(out['unavailable_chains'])}"
                   if out["unavailable_chains"] else "")
                + (f". Mandate readiness UNKNOWN: {reason}" if ready is None else "") + ".")
        return self._ar(content=text, metadata=meta)


__all__ = ["CROSSED", "DiscoveryCountParams", "MarksReadsMixin", "NEAR", "NONE",
           "PositionMarkParams", "UNKNOWN", "barrier_for"]
