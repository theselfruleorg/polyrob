"""Trading presets — the ONE place a trading mandate's numbers live (086 §3.4).

Skills name a preset; they never carry its numbers. A mandate (``core.trading_mandate``)
names a preset, may carry owner-tuned overrides, and the money kernel's mandate step
(``core.money.mandate_gate``) reads them.

These are ALLOCATION and PREFERENCE numbers, never caps: the per-transaction and
daily caps live only in the signer envelope (``tests/test_limit_single_source_ratchet.py``).
A ticket here is intent; the guard enforces the cap.

Units: every ``*_pct`` is a percentage (``-50.0`` = minus fifty per cent). A stop is
negative (below entry); the loss breakers are POSITIVE magnitudes of the allocation
(``daily_loss_pct=10`` = a realized loss of 10 % of the allocation in 24 h).

Load-time rules (:func:`validate_preset`, run on every preset below at import):

* a stop sits strictly between -100 % and entry (``stop_pct`` in (-100, 0));
* no take-profit tier fires at or below break-even (a break-even lock still pays fees);
* trailing arms only above entry, and the armed floor sits above entry too
  (``(1 + trail_arm_pct) * (1 - trail_pct) > 1``; a floor below entry ratchets a winner into a loss).

Who may change a number: the agent may only TIGHTEN (:func:`tighten`); the owner may
tune any number through the intake card (:func:`owner_tune`), and the result still
passes :func:`validate_preset`.

Tier-0: imports nothing above ``core``.
"""
from dataclasses import dataclass, fields, replace
from typing import Dict, Mapping, Optional, Tuple

STAGES = ("probe", "small", "normal")


@dataclass(frozen=True)
class ExitPreset:
    """The barriers an exit job arms for one position."""

    name: str
    #: The stop, in % from entry (negative). A stop only ever moves TIGHTER.
    stop_pct: float
    #: ``((gain_pct, sell_pct), ...)``: at +gain_pct sell sell_pct of the position.
    take_profit: Tuple[Tuple[float, float], ...] = ()
    #: Trail this many % below the high-water mark once armed. None = no trail.
    trail_pct: Optional[float] = None
    #: The trail arms once the position is this many % above entry.
    trail_arm_pct: Optional[float] = None
    #: Close after this many hours ...
    time_stop_h: Optional[float] = None
    #: ... unless the position ever reached this gain (None = close regardless).
    time_stop_min_gain_pct: Optional[float] = None
    #: The $0 precheck wakes the model when a barrier is within this many
    #: percentage points.
    near_pct: float = 5.0


@dataclass(frozen=True)
class BreakerSet:
    """Entry-only breakers. Loss figures are positive % of the allocation."""

    daily_loss_pct: Optional[float] = None
    drawdown_pct: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
    #: The pause after ``max_consecutive_losses`` (a cooldown expires; a latch does not).
    cooldown_h: Optional[float] = None
    max_entries_per_day: Optional[int] = None
    #: No re-entry into a token this many hours after it was sold.
    token_cooldown_h: Optional[float] = None
    #: The mandate's LLM spend above this % of the allocation trips a breaker (§3.7).
    cost_over_alloc_pct: Optional[float] = None


@dataclass(frozen=True)
class PromotionLadder:
    """Micro-live promotion (086 §3.8): probe -> small -> normal."""

    probe_ticket_usd: float = 1.0
    small_fraction: float = 0.5
    normal_fraction: float = 1.0
    #: ``normal`` needs at least this many closed trades at ``small`` ...
    normal_min_closed_trades: int = 5
    #: ... and realized P&L at ``small`` >= the added risk of ``normal``:
    #: (normal ticket - small ticket) x slots x |stop| (the agiprolabs ratchet).


@dataclass(frozen=True)
class Preset:
    name: str
    universe: str
    #: The default max cost per entry. None = derived from the allocation
    #: (``ticket_alloc_pct``).
    ticket_usd: Optional[float]
    slots: int
    hunt_cadence_min: int
    exit_cadence_min: Optional[int]
    exit: Optional[ExitPreset]
    breakers: BreakerSet
    #: ``((tier, max_ticket_usd), ...)`` — the sizing ladder under ``ticket_usd``.
    tier_ladder: Tuple[Tuple[str, float], ...] = ()
    #: ``(min_pct, max_pct)`` of the allocation per ticket (``majors``).
    ticket_alloc_pct: Optional[Tuple[float, float]] = None
    promotion: PromotionLadder = PromotionLadder()


MEMECOIN_EXIT = ExitPreset(
    name="memecoin_default", stop_pct=-50.0, take_profit=((100.0, 50.0),),
    trail_pct=40.0, trail_arm_pct=100.0, time_stop_h=48.0,
    time_stop_min_gain_pct=20.0, near_pct=10.0)

MAJORS_EXIT = ExitPreset(
    name="majors_default", stop_pct=-8.0, take_profit=((15.0, 50.0),),
    trail_pct=10.0, trail_arm_pct=12.0, time_stop_h=14 * 24.0,
    time_stop_min_gain_pct=None, near_pct=2.0)

EXIT_PRESETS: Dict[str, ExitPreset] = {e.name: e for e in (MEMECOIN_EXIT, MAJORS_EXIT)}

MEMECOIN = Preset(
    name="memecoin",
    universe=("new pairs hours to days old on the mandate chains that pass the hard "
              "screen and the survivability gates"),
    ticket_usd=3.0, slots=8, hunt_cadence_min=30, exit_cadence_min=15,
    exit=MEMECOIN_EXIT,
    breakers=BreakerSet(daily_loss_pct=10.0, drawdown_pct=30.0, max_consecutive_losses=4,
                        cooldown_h=6.0, max_entries_per_day=10, token_cooldown_h=24.0,
                        cost_over_alloc_pct=10.0),
    tier_ladder=(("A", 5.0), ("B", 2.5), ("C", 1.0)))

MAJORS = Preset(
    name="majors",
    universe="ETH, SOL, cbBTC/WBTC against the pinned stable",
    ticket_usd=None, slots=3, hunt_cadence_min=60, exit_cadence_min=60,
    exit=MAJORS_EXIT,
    breakers=BreakerSet(daily_loss_pct=3.0, drawdown_pct=15.0, max_consecutive_losses=3,
                        cooldown_h=24.0, max_entries_per_day=3, token_cooldown_h=24.0,
                        cost_over_alloc_pct=5.0),
    ticket_alloc_pct=(5.0, 10.0))

DCA = Preset(
    name="dca",
    universe="one pinned major, a fixed amount on a schedule",
    ticket_usd=None, slots=1, hunt_cadence_min=24 * 60, exit_cadence_min=None,
    exit=None,  # accumulate: no exit barriers
    breakers=BreakerSet(cost_over_alloc_pct=5.0))

PRESETS: Dict[str, Preset] = {p.name: p for p in (MEMECOIN, MAJORS, DCA)}


class PresetError(ValueError):
    """A preset (or a change to one) breaks a load-time rule."""


def validate_exit(e: ExitPreset) -> None:
    if not (-100.0 < float(e.stop_pct) < 0.0):
        raise PresetError(f"{e.name}: the stop must sit below entry and above -100 % "
                          f"(got {e.stop_pct})")
    sold = 0.0
    for gain, sell in e.take_profit:
        if float(gain) <= 0.0:
            raise PresetError(f"{e.name}: a take-profit tier at {gain} % locks at or below "
                              f"break-even (a break-even lock still pays fees)")
        if not (0.0 < float(sell) <= 100.0):
            raise PresetError(f"{e.name}: a tier sells 0-100 % of the position (got {sell})")
        sold += float(sell)
    if sold > 100.0:
        raise PresetError(f"{e.name}: the tiers sell more than the whole position")
    if (e.trail_pct is None) != (e.trail_arm_pct is None):
        raise PresetError(f"{e.name}: a trail needs both trail_pct and trail_arm_pct")
    if e.trail_pct is not None:
        if not (0.0 < float(e.trail_pct) < 100.0):
            raise PresetError(f"{e.name}: trail_pct must be in (0, 100)")
        if float(e.trail_arm_pct) <= 0.0:
            raise PresetError(f"{e.name}: the trail arms only above entry "
                              f"(trail_arm_pct must be > 0)")
        floor = (1.0 + float(e.trail_arm_pct) / 100.0) * (1.0 - float(e.trail_pct) / 100.0)
        if floor <= 1.0:
            raise PresetError(f"{e.name}: the armed trail floor ({floor:.3f} x entry) is not "
                              f"above entry — arm later or trail tighter")
    if e.time_stop_h is not None and float(e.time_stop_h) <= 0.0:
        raise PresetError(f"{e.name}: time_stop_h must be > 0")
    if float(e.near_pct) < 0.0:
        raise PresetError(f"{e.name}: near_pct must be >= 0")


def validate_breakers(b: BreakerSet, name: str = "") -> None:
    for f in fields(b):
        v = getattr(b, f.name)
        if v is not None and float(v) <= 0.0:
            raise PresetError(f"{name}: breaker {f.name} must be > 0 or unset (got {v})")
    for f in ("daily_loss_pct", "drawdown_pct", "cost_over_alloc_pct"):
        v = getattr(b, f)
        if v is not None and float(v) > 100.0:
            raise PresetError(f"{name}: breaker {f} is a % of the allocation (<= 100)")
    if b.max_consecutive_losses is not None and b.cooldown_h is None:
        raise PresetError(f"{name}: max_consecutive_losses needs cooldown_h")


def validate_preset(p: Preset) -> None:
    """Raise :class:`PresetError` when *p* breaks a load-time rule."""
    if p.ticket_usd is not None and float(p.ticket_usd) <= 0.0:
        raise PresetError(f"{p.name}: ticket_usd must be > 0")
    if int(p.slots) < 1:
        raise PresetError(f"{p.name}: slots must be >= 1")
    if int(p.hunt_cadence_min) < 1:
        raise PresetError(f"{p.name}: hunt_cadence_min must be >= 1")
    if p.exit is not None:
        validate_exit(p.exit)
        if p.exit_cadence_min is None or int(p.exit_cadence_min) < 1:
            raise PresetError(f"{p.name}: an exit preset needs exit_cadence_min >= 1")
    validate_breakers(p.breakers, p.name)
    for tier, usd in p.tier_ladder:
        if float(usd) <= 0.0:
            raise PresetError(f"{p.name}: tier {tier} ticket must be > 0")
    if p.ticket_alloc_pct is not None:
        lo, hi = p.ticket_alloc_pct
        if not (0.0 < float(lo) <= float(hi) <= 100.0):
            raise PresetError(f"{p.name}: ticket_alloc_pct must be 0 < min <= max <= 100")
    pr = p.promotion
    if not (0.0 < pr.probe_ticket_usd) or not (0.0 < pr.small_fraction <= pr.normal_fraction <= 1.0):
        raise PresetError(f"{p.name}: the promotion ladder must rise probe -> small -> normal")


for _p in PRESETS.values():
    validate_preset(_p)


def get_preset(name: str) -> Preset:
    try:
        return PRESETS[str(name or "").strip().lower()]
    except KeyError:
        raise PresetError(f"unknown preset {name!r} (known: {', '.join(PRESETS)})") from None


def default_ticket_usd(preset: Preset, budget_usd: float) -> float:
    """The preset's default max cost per entry for an allocation of *budget_usd*."""
    if preset.ticket_usd is not None:
        return float(preset.ticket_usd)
    if preset.ticket_alloc_pct is not None:
        return round(float(budget_usd) * float(preset.ticket_alloc_pct[1]) / 100.0, 2)
    return float(budget_usd)


# -- changing a preset's numbers ------------------------------------------------

#: field -> (where it lives, the direction that is TIGHTER: +1 = higher, -1 = lower).
_TIGHTER: Dict[str, Tuple[str, int]] = {
    "ticket_usd": ("preset", -1),
    "slots": ("preset", -1),
    "stop_pct": ("exit", +1),               # closer to entry
    "trail_pct": ("exit", -1),
    "trail_arm_pct": ("exit", -1),          # arms sooner (still > 0)
    "time_stop_h": ("exit", -1),
    "time_stop_min_gain_pct": ("exit", +1),
    "near_pct": ("exit", +1),               # wakes sooner
    "daily_loss_pct": ("breakers", -1),
    "drawdown_pct": ("breakers", -1),
    "max_consecutive_losses": ("breakers", -1),
    "cooldown_h": ("breakers", +1),
    "max_entries_per_day": ("breakers", -1),
    "token_cooldown_h": ("breakers", +1),
    "cost_over_alloc_pct": ("breakers", -1),
}
TUNABLE = frozenset(_TIGHTER)


def _apply(preset: Preset, overrides: Mapping[str, object], *, agent: bool) -> Preset:
    unknown = set(overrides) - TUNABLE
    if unknown:
        raise PresetError(f"not a tunable preset number: {', '.join(sorted(unknown))}")
    top: Dict[str, object] = {}
    ex: Dict[str, object] = {}
    br: Dict[str, object] = {}
    for key, new in overrides.items():
        where, tighter = _TIGHTER[key]
        target = {"preset": preset, "exit": preset.exit, "breakers": preset.breakers}[where]
        if target is None:
            raise PresetError(f"{preset.name}: has no exit preset to change ({key})")
        old = getattr(target, key)
        if agent:
            if new is None or old is None:
                if new != old:
                    raise PresetError(f"{key}: the agent may only tighten an existing number "
                                      f"({old!r} -> {new!r})")
            elif (float(new) - float(old)) * tighter < 0:
                raise PresetError(f"{key}: the agent may only tighten ({old} -> {new} loosens); "
                                  f"the owner tunes a preset through the card")
        {"preset": top, "exit": ex, "breakers": br}[where][key] = new
    out = preset
    if ex:
        out = replace(out, exit=replace(preset.exit, **ex))
    if br:
        out = replace(out, breakers=replace(preset.breakers, **br))
    if top:
        out = replace(out, **top)
    validate_preset(out)
    return out


def tighten(preset: Preset, **overrides) -> Preset:
    """The agent's path: every override must be at least as tight as the preset's
    number, and the result must pass :func:`validate_preset`. Raises
    :class:`PresetError` otherwise."""
    return _apply(preset, overrides, agent=True)


def owner_tune(preset: Preset, **overrides) -> Preset:
    """The owner's path (the intake card): any direction, still validated."""
    return _apply(preset, overrides, agent=False)


def stop_may_move(current_pct: float, proposed_pct: float) -> bool:
    """A position's stop only ever moves TIGHTER than where it sits (never wider
    than at entry)."""
    return float(proposed_pct) >= float(current_pct)
