"""086 W2: the trading presets — the ONE place the numbers live."""
import dataclasses

import pytest

from core import trading_presets as tp
from core.trading_presets import (DCA, MAJORS, MEMECOIN, PRESETS, ExitPreset, PresetError,
                                  default_ticket_usd, get_preset, owner_tune, stop_may_move,
                                  tighten, validate_exit, validate_preset)


def test_the_three_presets_exist_and_validate():
    assert set(PRESETS) == {"memecoin", "majors", "dca"}
    for p in PRESETS.values():
        validate_preset(p)


def test_memecoin_numbers_are_the_approved_ones():
    e, b = MEMECOIN.exit, MEMECOIN.breakers
    assert (MEMECOIN.ticket_usd, MEMECOIN.slots) == (3.0, 8)  # D3
    assert (MEMECOIN.hunt_cadence_min, MEMECOIN.exit_cadence_min) == (30, 15)
    assert e.name == "memecoin_default" and e.stop_pct == -50.0
    assert e.take_profit == ((100.0, 50.0),)
    assert (e.trail_pct, e.trail_arm_pct) == (40.0, 100.0)
    assert (e.time_stop_h, e.time_stop_min_gain_pct) == (48.0, 20.0)
    assert (b.daily_loss_pct, b.drawdown_pct, b.max_consecutive_losses, b.cooldown_h,
            b.max_entries_per_day, b.token_cooldown_h) == (10.0, 30.0, 4, 6.0, 10, 24.0)
    assert dict(MEMECOIN.tier_ladder) == {"A": 5.0, "B": 2.5, "C": 1.0}


def test_majors_numbers_are_the_approved_ones():
    e, b = MAJORS.exit, MAJORS.breakers
    assert MAJORS.slots == 3 and MAJORS.ticket_alloc_pct == (5.0, 10.0)
    assert (e.stop_pct, e.take_profit, e.trail_pct, e.trail_arm_pct) == (
        -8.0, ((15.0, 50.0),), 10.0, 12.0)
    assert e.time_stop_h == 14 * 24.0
    assert (b.daily_loss_pct, b.drawdown_pct, b.max_consecutive_losses, b.cooldown_h) == (
        3.0, 15.0, 3, 24.0)
    assert default_ticket_usd(MAJORS, 150.0) == 15.0


def test_dca_has_no_exit():
    assert DCA.exit is None and DCA.slots == 1


def test_unknown_preset_refuses():
    with pytest.raises(PresetError):
        get_preset("yolo")
    assert get_preset(" Memecoin ") is MEMECOIN


@pytest.mark.parametrize("change", [
    {"stop_pct": 0.0}, {"stop_pct": 5.0}, {"stop_pct": -100.0},
    {"take_profit": ((0.0, 50.0),)}, {"take_profit": ((-5.0, 50.0),)},
    {"trail_arm_pct": 0.0}, {"trail_arm_pct": -10.0},
    {"trail_arm_pct": None},
    {"trail_arm_pct": 10.0, "trail_pct": 10.0},   # armed floor 0.99 x entry
    {"trail_arm_pct": 50.0, "trail_pct": 40.0},   # armed floor 0.90 x entry
])
def test_load_time_rules_refuse(change):
    with pytest.raises(PresetError):
        validate_exit(dataclasses.replace(tp.MEMECOIN_EXIT, **change))


def test_agent_may_tighten():
    p = tighten(MEMECOIN, stop_pct=-30.0, daily_loss_pct=5.0, slots=4, cooldown_h=12.0,
                token_cooldown_h=48.0, max_entries_per_day=5)
    assert p.exit.stop_pct == -30.0 and p.breakers.daily_loss_pct == 5.0 and p.slots == 4
    assert MEMECOIN.exit.stop_pct == -50.0  # the shared preset is untouched


@pytest.mark.parametrize("change", [
    {"stop_pct": -60.0}, {"daily_loss_pct": 20.0}, {"slots": 10}, {"ticket_usd": 5.0},
    {"cooldown_h": 1.0}, {"trail_pct": 50.0}, {"time_stop_h": 96.0},
    {"max_entries_per_day": 20}, {"token_cooldown_h": 1.0},
])
def test_agent_may_not_loosen(change):
    with pytest.raises(PresetError, match="only tighten"):
        tighten(MEMECOIN, **change)


def test_agent_tighten_still_validates():
    with pytest.raises(PresetError):
        tighten(MEMECOIN, stop_pct=0.0)  # tighter, but no longer a stop


def test_unknown_field_refuses():
    with pytest.raises(PresetError, match="tunable"):
        tighten(MEMECOIN, hunt_cadence_min=1)
    with pytest.raises(PresetError, match="tunable"):
        owner_tune(MEMECOIN, name="x")


def test_owner_may_loosen_but_not_break_the_rules():
    p = owner_tune(MEMECOIN, stop_pct=-60.0, daily_loss_pct=15.0, slots=10)
    assert (p.exit.stop_pct, p.breakers.daily_loss_pct, p.slots) == (-60.0, 15.0, 10)
    with pytest.raises(PresetError):
        owner_tune(MEMECOIN, stop_pct=1.0)
    with pytest.raises(PresetError):
        owner_tune(MEMECOIN, trail_arm_pct=0.0)


def test_dca_has_no_exit_to_tune():
    with pytest.raises(PresetError):
        owner_tune(DCA, stop_pct=-10.0)


def test_a_stop_only_moves_tighter():
    assert stop_may_move(-50.0, -30.0)
    assert stop_may_move(-50.0, -50.0)
    assert not stop_may_move(-50.0, -60.0)


def test_presets_hold_no_cap_flag():
    import inspect
    src = inspect.getsource(tp)
    for flag in ("AGENT_WALLET_MAX_PER_TX_USD", "WALLET_DAILY_CAP_USD", "DEFI_AUTONOMOUS_MAX_USD"):
        assert flag not in src
