"""Task 8: ``ui.show_avatar`` preference — schema entry + resolver behavior."""
from core import prefs


def test_show_avatar_in_schema_safe_bool():
    spec = prefs.PREF_SCHEMA.get("ui.show_avatar")
    assert spec is not None
    assert spec.type == "bool"
    assert spec.sensitivity == prefs.SENSITIVITY_SAFE
    assert spec.merge == "override"
    assert spec.applies == "live"


def test_show_avatar_resolves_default_true(tmp_path):
    assert prefs.resolve("ui.show_avatar", "u1", tmp_path, env_value=None, default=True) is True


def test_show_avatar_pref_false_wins(tmp_path):
    ok, err = prefs.write_preference(tmp_path, "u1", "ui.show_avatar", False, "rob")
    assert ok, err
    assert prefs.resolve("ui.show_avatar", "u1", tmp_path, env_value=None, default=True) is False
