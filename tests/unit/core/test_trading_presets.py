"""086 W2: the trading presets — the ONE place the numbers live. 095 B1: core ships
generic presets only, a registry and an install's own file; no default preset."""
import dataclasses
import json

import pytest

from core import trading_presets as tp
from core.trading_presets import (DCA, MAJORS, PRESETS, ExitPreset, PresetError,
                                  default_ticket_usd, get_preset, owner_tune, stop_may_move,
                                  tighten, validate_exit, validate_preset)
from tests.trading_fixture import LONGTAIL, LONGTAIL_EXIT

MEMECOIN = LONGTAIL  # the wide long-tail shape the rules below are tested on


def test_core_ships_generic_presets_only():
    assert {"majors", "dca"} <= set(PRESETS)
    for name in ("majors", "dca"):
        validate_preset(PRESETS[name])
    src = open(tp.__file__, encoding="utf-8").read().lower()
    assert "memecoin" not in src  # an install's programme preset is not core's
    assert DCA.doctrine_skill == "dca" and MAJORS.doctrine_skill is None


def test_register_preset_validates_and_protects_the_core_names():
    with pytest.raises(PresetError):
        tp.register_preset(dataclasses.replace(LONGTAIL, name="broken", slots=0))
    with pytest.raises(PresetError, match="core preset"):
        tp.register_preset(dataclasses.replace(LONGTAIL, name="majors"))
    assert get_preset("longtail") is LONGTAIL
    assert tp.get_exit_preset("longtail_exit") is LONGTAIL_EXIT


def _instance_file(tmp_path, body):
    path = tmp_path / tp.INSTANCE_FILE
    path.write_text(json.dumps(body) if not isinstance(body, str) else body)
    return str(path)


_INST = {"presets": [{
    "name": "hunt", "universe": "u", "ticket_usd": 2, "slots": 4, "hunt_cadence_min": 30,
    "exit_cadence_min": 15, "doctrine_skill": "my-hunt",
    "exit": {"name": "hunt_exit", "stop_pct": -40, "take_profit": [[80, 50]],
             "trail_pct": 30, "trail_arm_pct": 80, "time_stop_h": 24, "near_pct": 8},
    "breakers": {"daily_loss_pct": 10, "max_consecutive_losses": 3, "cooldown_h": 6},
    "tier_ladder": [["A", 4], ["B", 2], ["C", 1]]}],
    "screen": {"pool": {"liquidity_floor_usd": 1234, "bogus": 1},
               "survival": {"tier_a_age_h": 72}}}


def test_an_install_defines_its_own_presets_and_screen_lines(tmp_path):
    path = _instance_file(tmp_path, _INST)
    p = get_preset("Hunt", path=path)
    assert (p.ticket_usd, p.slots, p.doctrine_skill, p.exit.stop_pct) == (2.0, 4, "my-hunt",
                                                                          -40.0)
    assert dict(p.tier_ladder) == {"A": 4.0, "B": 2.0, "C": 1.0}
    assert "hunt" in tp.all_presets(path) and tp.get_exit_preset("hunt_exit", path=path)
    lines = tp.screen_lines("pool", {"liquidity_floor_usd": 500_000.0, "age": 12.0},
                            path=path)
    assert lines == {"liquidity_floor_usd": 1234.0, "age": 12.0}  # unknown key ignored
    assert tp.screen_lines("survival", {"tier_a_age_h": 48.0}, path=path) == {
        "tier_a_age_h": 72.0}


def test_an_unknown_preset_refuses_with_the_remedy(tmp_path):
    path = str(tmp_path / tp.INSTANCE_FILE)  # absent
    with pytest.raises(PresetError, match="trading_presets.json"):
        get_preset("yolo", path=path)


def test_a_broken_instance_file_adds_nothing_and_names_itself(tmp_path):
    bad = dict(_INST, presets=[dict(_INST["presets"][0], slots=0)])
    path = _instance_file(tmp_path, bad)
    with pytest.raises(PresetError, match="slots"):
        get_preset("hunt", path=path)
    path = _instance_file(tmp_path, "{not json")
    with pytest.raises(PresetError, match="trading_presets.json"):
        get_preset("hunt", path=path)
    assert tp.screen_lines("pool", {"liquidity_floor_usd": 5.0}, path=path) == {
        "liquidity_floor_usd": 5.0}
    shadow = dict(_INST, presets=[dict(_INST["presets"][0], name="majors")])
    path = _instance_file(tmp_path, shadow)
    assert get_preset("majors", path=path) is MAJORS


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


def test_preset_names_are_case_and_space_blind():
    assert get_preset(" Majors ") is MAJORS


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
        validate_exit(dataclasses.replace(LONGTAIL_EXIT, **change))


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


# --- the screen lines fail safe (review fix) ----------------------------------

def test_one_bad_preset_never_drops_the_screen_lines(tmp_path):
    bad = dict(_INST, presets=[dict(_INST["presets"][0], slots=0)])
    path = _instance_file(tmp_path, bad)
    with pytest.raises(PresetError, match="slots"):
        get_preset("hunt", path=path)
    lines, err = tp.screen_lines_checked("pool", {"liquidity_floor_usd": 5.0}, path=path)
    assert lines == {"liquidity_floor_usd": 1234.0} and err is None


def test_an_unreadable_file_reports_a_screen_error(tmp_path):
    path = _instance_file(tmp_path, "{not json")
    lines, err = tp.screen_lines_checked("pool", {"liquidity_floor_usd": 5.0}, path=path)
    assert lines == {"liquidity_floor_usd": 5.0}
    assert err and err.startswith("custom screen lines unreadable")
    path = _instance_file(tmp_path, {"screen": ["not", "an", "object"]})
    assert tp.screen_lines_checked("pool", {"x": 1.0}, path=path)[1]
    path = str(tmp_path / "absent.json")
    assert tp.screen_lines_checked("pool", {"x": 1.0}, path=path) == ({"x": 1.0}, None)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_screen_line_is_rejected(tmp_path, value):
    path = _instance_file(tmp_path, '{"screen": {"pool": {"liquidity_floor_usd": %s}}}'
                          % value)
    lines, err = tp.screen_lines_checked("pool", {"liquidity_floor_usd": 5.0}, path=path)
    assert lines == {"liquidity_floor_usd": 5.0}
    assert err and "not a finite number" in err


def _broken_install(tmp_path, monkeypatch):
    path = _instance_file(tmp_path, "{not json")
    monkeypatch.setattr(tp, "instance_presets_path", lambda: path)


def test_pool_screen_is_unscreenable_on_unreadable_lines(tmp_path, monkeypatch):
    import types
    from tools.defi import pool_screen
    _broken_install(tmp_path, monkeypatch)
    pool = types.SimpleNamespace(name="A / B", liquidity_usd=900_000.0,
                                 volume_h24_usd=1_000_000.0, vol_liq_ratio=1.1,
                                 hourly_vol_liq_ratio=0.1, txns_per_buyer=2.0,
                                 age_hours=100.0)
    v = pool_screen.classify(pool)
    assert v.verdict == "UNSCREENABLE"
    assert "custom screen lines unreadable" in v.reasons[0]


def test_survivability_is_partial_on_unreadable_lines(tmp_path, monkeypatch):
    from tools.defi import survivability as sv
    _broken_install(tmp_path, monkeypatch)
    res = sv.assess("evm", screen=None, holders=None, quote=None)
    assert res["screen_lines_error"].startswith("custom screen lines unreadable")
    assert "screen_lines" in res["unknown"]
    assert res["tier_ceiling"] in ("C", sv.REJECT)
    assert any("PARTIAL" in line for line in sv.render(res))
