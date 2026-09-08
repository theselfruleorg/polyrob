"""030 WS-E5: the effective-posture card — one builder, all capability axes."""
import pytest

import agents.task.constants as constants
from core.config_policy.posture_card import build_posture_card, render_posture_card


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("POLYROB_LOCAL", "AUTONOMY_ENABLED", "AUTONOMY_MODE",
              "AUTONOMY_POSTURE", "AGENT_COMPUTE_POSTURE", "POLYROB_POSTURE",
              "AUTONOMY_HALT", "POLYROB_OWNER_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    constants._refreeze_compute_posture_for_tests()
    yield
    constants._refreeze_compute_posture_for_tests()


def _axes(rows):
    return {r["axis"]: r for r in rows}


def test_card_covers_all_five_axes_plus_owner_pause():
    axes = _axes(build_posture_card())
    for want in ("trust profile (local mode)", "autonomy master",
                 "capability mode", "loop posture", "compute posture",
                 "owner pause"):
        assert want in axes, f"missing axis: {want}"


def test_defaults_read_as_defaults():
    axes = _axes(build_posture_card())
    assert axes["capability mode"]["effective"] == "supervised"
    assert axes["capability mode"]["source"] == "default"
    assert axes["compute posture"]["effective"] == 0


def test_clamped_autonomous_mode_is_called_out(monkeypatch):
    # autonomous requested but no POLYROB_LOCAL/owner binding -> clamp is VISIBLE
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    axes = _axes(build_posture_card())
    assert axes["capability mode"]["effective"] == "supervised"
    assert "CLAMPED" in axes["capability mode"]["note"]


def test_inert_compute_posture_env_is_called_out(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    # frozen value stays whatever import froze (0 in the clean test env)
    axes = _axes(build_posture_card())
    if axes["compute posture"]["effective"] != 2:
        assert "INERT" in axes["compute posture"]["note"]


def test_render_is_one_line_per_axis():
    rows = build_posture_card()
    lines = render_posture_card(rows)
    assert len(lines) == len(rows)
    assert all(":" in ln and "[" in ln for ln in lines)
