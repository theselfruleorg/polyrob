"""What may I spend right now — the ONE read of the live spend limits.

Prod 2026-10-10..13 (review 2026-10-13 C-01/B5): five homes held five numbers —
an identity doc's "$10/tx, $20/day", the per-transaction cap ($200, PolicyGate),
the no-ask ceiling ($300 pref), the unchecked-route ticket ($5) and the wallet
prefs — and the agent rebuilt "the cap" from memory and prose. In 61 seconds it
named four different causes for one refusal; a scheduled buy declared the doc's $10
and was refused.

This module renders the limits the guard enforces, READ THROUGH THE GUARD'S OWN
OBJECTS AND RESOLVERS — never a second copy of a number:

* per-transaction and rolling-24h caps: the process spend ledger
  (``core.money.hooks.get_spend_ledger`` → ``SpendLedger.per_tx_cap_usd`` /
  ``daily_cap_usd`` / ``rolling_24h_spend_usd``), the object ``check`` reads;
* the no-ask (autonomous) ceiling: ``core.config_policy.spend_lane.autonomous_ceiling_usd``,
  which delegates to ``tx_guard.autonomous_max_usd`` — the function tx_guard's
  value step calls;
* the money regime line: ``core.config_policy.money_regime.money_regime_display``;
* the unchecked-route ticket: :data:`UNVERIFIED_UNCHECKED_MAX_USD`, the ONE copy
  (``tools.defi.identity_gate`` and ``tools.defi.buy_screen`` import it).

Every leg fails open to "unreadable" — an unread cap is never shown as a number
and never as "no cap". Never raises.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: The largest ticket (USD) a buy may carry when no independent price agrees
#: with the route and the token is not trusted (owner pin, own launch, the
#: run's owner target). The ONE copy: ``tools.defi.identity_gate`` re-exports it.
UNVERIFIED_UNCHECKED_MAX_USD = 5.0


@dataclass
class SpendLimits:
    """The live limits. ``None`` on a leg = unreadable (see ``errors``), except
    ``daily_usd`` where ``daily_disabled`` tells "no cap" apart from unread."""
    per_tx_usd: Optional[float] = None
    daily_usd: Optional[float] = None
    daily_disabled: bool = False
    used_24h_usd: Optional[float] = None
    ask_above_usd: Optional[float] = None
    #: ``spend_lane.tiered_spend_lane_enabled()`` — the switch the approval hook's
    #: exemption reads. OFF (the default) = every live on-chain spend asks the
    #: owner, whatever the ceiling says; ``None`` = unreadable.
    tiered_lane: Optional[bool] = None
    regime: str = ""
    unchecked_route_usd: float = UNVERIFIED_UNCHECKED_MAX_USD
    errors: Dict[str, str] = field(default_factory=dict)


def _finite(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def spend_limits_now() -> SpendLimits:
    """Read every leg from the object or function the guard itself uses."""
    lim = SpendLimits()
    try:
        from core.money.hooks import get_spend_ledger
        gate = get_spend_ledger()
        if gate is None:
            lim.errors["caps"] = "no spend ledger registered"
        else:
            lim.per_tx_usd = _finite(gate.per_tx_cap_usd)
            daily = gate.daily_cap_usd
            if daily is None:
                lim.daily_disabled = True
            else:
                lim.daily_usd = _finite(daily)
            try:
                sync = getattr(gate, "_sync_shared_ledger", None)
                if callable(sync):
                    sync()
                lim.used_24h_usd = _finite(gate.rolling_24h_spend_usd())
            except Exception as exc:  # noqa: BLE001
                lim.errors["used"] = f"spend ledger unreadable: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        lim.errors["caps"] = f"caps unreadable: {type(exc).__name__}"
    try:
        # The seam that delegates to tx_guard's own resolver (the kernel may not
        # import the wallet); an unresolved ceiling reads 0.0 there — every
        # unattended spend asks, which is what the lane then does.
        from core.config_policy.spend_lane import autonomous_ceiling_usd
        lim.ask_above_usd = _finite(autonomous_ceiling_usd())
    except Exception as exc:  # noqa: BLE001
        lim.errors["ask_above"] = f"ceiling unreadable: {type(exc).__name__}"
    try:
        from core.config_policy.spend_lane import tiered_spend_lane_enabled
        lim.tiered_lane = bool(tiered_spend_lane_enabled())
    except Exception as exc:  # noqa: BLE001
        lim.errors["lane"] = f"spend lane unreadable: {type(exc).__name__}"
    try:
        from core.config_policy.money_regime import money_regime_display
        lim.regime = str(money_regime_display() or "")
    except Exception as exc:  # noqa: BLE001
        lim.errors["regime"] = f"regime unreadable: {type(exc).__name__}"
    return lim


def _usd(value: Optional[float]) -> str:
    return f"${value:,.2f}"


def render_spend_limits(lim: SpendLimits) -> List[str]:
    """The agent-facing block. Lines start with ``- `` under one header."""
    from core.money import PER_TX_CAP_REMEDY
    out = ["Spend limits now (live, read from the guard itself). These numbers "
           "OVERRIDE any spend figure in identity docs, memory or earlier messages:"]
    if lim.per_tx_usd is not None:
        out.append(f"- per-transaction cap: {_usd(lim.per_tx_usd)} — every spend above it "
                   f"is refused, the owner's own taps included; the owner raises it: "
                   f"{PER_TX_CAP_REMEDY}")
    else:
        out.append("- per-transaction cap: unreadable ("
                   f"{lim.errors.get('caps', 'not read')}) — do not state a number")
    if lim.daily_disabled:
        out.append("- rolling 24h cap: none (the operator disabled it)")
    elif lim.daily_usd is not None:
        used = ("used: unreadable" if lim.used_24h_usd is None else
                f"used {_usd(lim.used_24h_usd)}, left "
                f"{_usd(max(0.0, lim.daily_usd - lim.used_24h_usd))}")
        out.append(f"- rolling 24h cap: {_usd(lim.daily_usd)} ({used})")
    else:
        out.append("- rolling 24h cap: unreadable — do not state a number")
    if lim.tiered_lane is False:
        # The approval hook exempts a within-ceiling spend ONLY under the tiered
        # lane (spend_lane.spend_exemption); with it off the ceiling routes nothing.
        out.append("- no-ask ceiling: none in effect — the tiered spend lane "
                   "(DEFI_TIERED_SPEND_LANE) is off, so EVERY live on-chain spend needs "
                   "the owner's approval (lane owner_queue), whatever its size; a dry "
                   "run never asks")
    elif lim.ask_above_usd is not None and lim.tiered_lane is True:
        out.append(f"- no-ask ceiling: {_usd(lim.ask_above_usd)} per transaction — an "
                   "unattended spend above it is NOT SENT and needs the owner's approval "
                   "(lane owner_queue); the owner's own command is not held to it")
    else:
        out.append("- no-ask ceiling: unreadable ("
                   f"{lim.errors.get('ask_above') or lim.errors.get('lane') or 'not read'}) — "
                   "treat every live spend "
                   "as needing the owner")
    if lim.regime:
        out.append(f"- money regime: {lim.regime}")
    out.append(f"- unchecked route: {_usd(lim.unchecked_route_usd)} — a buy whose route no "
               "independent price confirms, unless the token is trusted (owner pin, own "
               "launch, or the owner's target); the owner pins one with `/wallet trust "
               "<chain> <address> <SYMBOL> go` — no setting raises this limit")
    out.append("- max_spend_usd: declare the spend's real USD value (at most the "
               "per-transaction cap), never a number from a document")
    out.append("A refusal names its rule — quote it, do not guess: 'per-transaction cap' "
               "/ 'daily spend cap' = the caps above; 'autonomous ceiling' or lane "
               "owner_queue = the no-ask ceiling; 'declared max_spend_usd' = your own "
               "declaration; 'unchecked route' = the route ticket; 'approval denied' = the "
               "owner queue; 'a money action was refused in this run' = this run is held "
               "from public posts. Compare a spend with a cap only through a tool-computed "
               "USD value (the guard's simulated value, defi_data.swap_quote) — never a "
               "hand conversion.")
    return out


def spend_limits_lines() -> List[str]:
    """``render_spend_limits(spend_limits_now())``; one honest line on any fault."""
    try:
        return render_spend_limits(spend_limits_now())
    except Exception as exc:  # noqa: BLE001
        logger.debug("spend limits render failed", exc_info=True)
        return [f"Spend limits: unavailable ({type(exc).__name__}) — do not state a cap "
                "from memory or a document; say it is unknown."]


__all__ = ["SpendLimits", "UNVERIFIED_UNCHECKED_MAX_USD", "render_spend_limits",
           "spend_limits_lines", "spend_limits_now"]
