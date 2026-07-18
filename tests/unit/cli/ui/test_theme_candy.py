"""Theme candy vocabulary: new glyphs, style roles, state_glyph mapping."""
from cli.ui.theme import ICONS, STYLES, state_glyph


def test_new_icons_present():
    assert ICONS.speaker == "●"
    assert ICONS.working == "⋯"
    assert ICONS.cooking == "✱"
    assert ICONS.autonomy == "⟲"
    assert ICONS.pending == "○"
    assert ICONS.tree == "└"


def test_new_style_roles():
    assert STYLES.label == "dim"
    assert STYLES.value == ""
    assert STYLES.accent == "cyan"
    assert STYLES.warn == "yellow"
    assert STYLES.subagent_name == "dim bold"


def test_state_glyph_known_states():
    assert state_glyph("running") == ("●", "status_running")
    assert state_glyph("done") == ("✓", "status_ok")
    assert state_glyph("completed") == ("✓", "status_ok")
    assert state_glyph("failed") == ("✗", "status_error")
    assert state_glyph("error") == ("✗", "status_error")
    assert state_glyph("blocked") == ("⚠", "warn")
    assert state_glyph("pending") == ("○", "meta")
    assert state_glyph("open") == ("○", "meta")
    assert state_glyph("ready") == ("○", "meta")
    assert state_glyph("timeout") == ("⚠", "warn")
    assert state_glyph("scheduled") == ("○", "meta")
    assert state_glyph("connected") == ("✓", "status_ok")
    assert state_glyph("disconnected") == ("✗", "status_error")
    assert state_glyph("connecting") == ("●", "status_running")
    assert state_glyph("reconnecting") == ("●", "status_running")
    assert state_glyph("enabled") == ("✓", "status_ok")
    assert state_glyph("disabled") == ("○", "meta")


def test_state_glyph_unknown_and_edge():
    assert state_glyph("weird-state") == ("·", "meta")
    assert state_glyph("") == ("·", "meta")
    assert state_glyph(None) == ("·", "meta")
    assert state_glyph("  Running  ") == ("●", "status_running")  # trims + lowers
