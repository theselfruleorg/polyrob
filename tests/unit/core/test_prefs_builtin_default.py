"""P0.1 (proposal 018): the /config panel shows the REAL effective default.

``display_effective`` historically resolved the no-env/no-pref case with
``default=None``, rendering a wall of ``None (default)`` rows even though every
enforcement site has a real operational default. These tests pin the honest
display: env-backed keys fall back to the flags-catalog default (posture/local
aware via ``dynamic_flag_default``), pure prefs carry a static
``default_display``, and keys with genuinely no default keep the legacy
``(None, "default")`` shape. Display-only — the resolver merge inputs are
unchanged (guarded by the last test).
"""
import pytest

from core.prefs import PREF_SCHEMA, display_effective, resolve_with_source, write_preference


@pytest.fixture()
def home(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Keys under test must have their env flags unset so the fallback path runs.
    for var in ("GOAL_DAILY_QUOTA", "GOAL_NOTIFY_ON_DONE", "SELF_WAKE_ENABLED",
                "POLYROB_LOCAL", "ROB_LOCAL", "AUTONOMY_POSTURE"):
        monkeypatch.delenv(var, raising=False)


def test_env_backed_key_shows_catalog_default_when_unset(home):
    val, src = display_effective("goals.daily_quota", "u1", home)
    assert src == "built-in"
    assert isinstance(val, int) and val == 6  # docs/CONFIGURATION.md default


def test_dynamic_default_is_posture_aware(home, monkeypatch):
    # SELF_WAKE_ENABLED is in _SAFE_LOCAL_FLAGS: OFF on a server, ON under local.
    val, src = display_effective("autonomy.self_wake", "u1", home)
    assert val is False and src.startswith("default(")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    val, src = display_effective("autonomy.self_wake", "u1", home)
    assert val is True and "local=ON" in src


def test_pure_pref_key_shows_static_default(home):
    assert display_effective("digest.channel", "u1", home) == ("telegram", "built-in")
    assert display_effective("ui.show_avatar", "u1", home) == (True, "built-in")


def test_no_default_key_keeps_legacy_none(home):
    # style.* have no operational default — "no preference set" is the truth.
    assert display_effective("style.tone", "u1", home) == (None, "default")


def test_pref_still_beats_builtin(home):
    ok, err = write_preference(home, "u1", "goals.daily_quota", 3)
    assert ok, err
    assert display_effective("goals.daily_quota", "u1", home) == (3, "pref")


def test_env_still_beats_builtin(home, monkeypatch):
    monkeypatch.setenv("GOAL_DAILY_QUOTA", "4")
    val, src = display_effective("goals.daily_quota", "u1", home)
    assert val == 4 and src == "env"


def test_every_env_backed_pref_key_is_catalog_registered():
    # The honest-default fallback resolves through core.flags — every
    # PREF_SCHEMA env_flag must therefore be a documented catalog row.
    from core.flags import REGISTRY
    missing = [s.env_flag for s in PREF_SCHEMA.values()
               if s.env_flag and s.env_flag not in REGISTRY]
    assert not missing, f"pref env_flags absent from flags catalog: {missing}"


def test_resolver_merge_inputs_unchanged(home):
    # Display-only guard: resolve_with_source keeps its bare-default semantics.
    val, src = resolve_with_source("goals.daily_quota", "u1", home,
                                   env_value=None, default=None)
    assert val is None and src == "default"
