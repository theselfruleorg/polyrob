"""A test-only trading preset: core ships generic presets only (``majors``, ``dca``)
and no default, so the mandate tests register this wide long-tail shape through the
same seam an install uses (``core.trading_presets.register_preset``).

Import it (``from tests.trading_fixture import LONGTAIL``) — the import registers it.
"""
from core.trading_presets import BreakerSet, ExitPreset, Preset, register_preset

LONGTAIL_EXIT = ExitPreset(
    name="longtail_exit", stop_pct=-50.0, take_profit=((100.0, 50.0),),
    trail_pct=40.0, trail_arm_pct=100.0, time_stop_h=48.0,
    time_stop_min_gain_pct=20.0, near_pct=10.0)

LONGTAIL = register_preset(Preset(
    name="longtail",
    universe="test fixture: new pairs on the mandate chains",
    ticket_usd=3.0, slots=8, hunt_cadence_min=30, exit_cadence_min=15,
    exit=LONGTAIL_EXIT,
    breakers=BreakerSet(daily_loss_pct=10.0, drawdown_pct=30.0, max_consecutive_losses=4,
                        cooldown_h=6.0, max_entries_per_day=10, token_cooldown_h=24.0,
                        cost_over_alloc_pct=10.0),
    tier_ladder=(("A", 5.0), ("B", 2.5), ("C", 1.0)),
    doctrine_skill="sizing-and-risk"))
