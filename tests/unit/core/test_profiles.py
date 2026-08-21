"""core/profiles.py — named-profile resolution + activation (multi-instance W2).

Every test isolates via POLYROB_HOME (tmp_path) so no test can ever touch the
developer's real ~/.polyrob/profiles/*.
"""
import os
from pathlib import Path

import pytest

import core.profiles as profiles
from core.profiles import (
    InvalidProfileNameError,
    ProfileNotFoundError,
    activate_profile,
    apply_profile_env,
    is_safe_profile_name,
    list_profiles,
    profile_dir,
    profiles_root,
    resolve_active_profile,
    warn_profile_fallback_once,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    base = tmp_path / "home"
    base.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(base))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE_SOURCE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILES_ROOT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE_RESOLVED", raising=False)
    # Run from a pin-free, git-free cwd so the project-pin tier is inert.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    # Reset the one-shot warning latches.
    monkeypatch.setattr(profiles, "_warned_mismatch", False)
    monkeypatch.setattr(profiles, "_warned_fallback", False)
    return base


def _mk_profile(base, name):
    d = base / "profiles" / name
    d.mkdir(parents=True)
    return d


# ── name safety ─────────────────────────────────────────────────────────────


def test_traversal_shaped_names_rejected():
    for bad in ("../x", "a/b", "a\\b", ".", "..", "", None, "a" * 65, "-lead"):
        assert not is_safe_profile_name(bad)
    with pytest.raises(InvalidProfileNameError):
        profile_dir("../x")


def test_reserved_names_rejected():
    for bad in ("default", "profiles", "data"):
        assert not is_safe_profile_name(bad)


def test_good_names_accepted():
    for good in ("rob", "scout", "my-bot_2", "A"):
        assert is_safe_profile_name(good)


# ── resolution tiers ────────────────────────────────────────────────────────


def test_no_selection_is_legacy_mode(_isolated):
    assert resolve_active_profile() is None


def test_flag_tier_selects(_isolated):
    _mk_profile(_isolated, "scout")
    sel = resolve_active_profile("scout")
    assert (sel.name, sel.source) == ("scout", "flag")
    assert sel.home == _isolated / "profiles" / "scout"


def test_flag_unknown_profile_errors_with_remedy(_isolated):
    with pytest.raises(ProfileNotFoundError) as exc:
        resolve_active_profile("ghost")
    assert "polyrob profile create ghost" in str(exc.value)


def test_env_tier_selects(_isolated, monkeypatch):
    _mk_profile(_isolated, "scout")
    monkeypatch.setenv("POLYROB_PROFILE", "scout")
    sel = resolve_active_profile()
    assert (sel.name, sel.source) == ("scout", "env")


def test_flag_beats_env(_isolated, monkeypatch):
    _mk_profile(_isolated, "scout")
    _mk_profile(_isolated, "envy")
    monkeypatch.setenv("POLYROB_PROFILE", "envy")
    assert resolve_active_profile("scout").name == "scout"


def test_empty_env_profile_forces_legacy_even_with_sticky(_isolated, monkeypatch):
    _mk_profile(_isolated, "scout")
    (_isolated / "active_profile").write_text("scout\n")
    monkeypatch.setenv("POLYROB_PROFILE", "")
    assert resolve_active_profile() is None


def test_project_pin_tier_walks_up_to_git_root(_isolated, monkeypatch, tmp_path):
    _mk_profile(_isolated, "pinned")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".polyrob").mkdir()
    (repo / ".polyrob" / "profile").write_text("pinned\n")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    sel = resolve_active_profile()
    assert (sel.name, sel.source) == ("pinned", "project_pin")


def test_pin_does_not_wander_past_git_root(_isolated, monkeypatch, tmp_path):
    _mk_profile(_isolated, "outer")
    outer = tmp_path / "outer"
    (outer / ".polyrob").mkdir(parents=True)
    (outer / ".polyrob" / "profile").write_text("outer\n")
    repo = outer / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    assert resolve_active_profile() is None  # the pin above the git root is invisible


def test_sticky_tier_selects(_isolated):
    _mk_profile(_isolated, "sticky1")
    (_isolated / "active_profile").write_text("sticky1\n")
    sel = resolve_active_profile()
    assert (sel.name, sel.source) == ("sticky1", "sticky")


def test_pin_beats_sticky(_isolated, monkeypatch, tmp_path):
    _mk_profile(_isolated, "pinned")
    _mk_profile(_isolated, "sticky1")
    (_isolated / "active_profile").write_text("sticky1\n")
    proj = tmp_path / "proj"
    (proj / ".polyrob").mkdir(parents=True)
    (proj / ".polyrob" / "profile").write_text("pinned\n")
    monkeypatch.chdir(proj)
    assert resolve_active_profile().name == "pinned"


def test_broken_sticky_warns_and_falls_through(_isolated, capsys):
    (_isolated / "active_profile").write_text("no-such-profile\n")
    assert resolve_active_profile() is None
    err = capsys.readouterr().err
    assert "no-such-profile" in err
    assert "polyrob profile create" in err


# ── apply semantics ─────────────────────────────────────────────────────────


def test_flag_overwrites_exported_homes(_isolated, monkeypatch):
    # `rob -P scout` inside a shell exporting another profile's home MUST run scout.
    home = _mk_profile(_isolated, "scout")
    monkeypatch.setenv("POLYROB_HOME", str(_isolated))  # base (as the wrapper exports)
    monkeypatch.setenv("POLYROB_DATA_DIR", "/somewhere/else")
    sel = resolve_active_profile("scout")
    apply_profile_env(sel)
    assert os.environ["POLYROB_HOME"] == str(home)
    assert os.environ["POLYROB_DATA_DIR"] == str(home / "data")
    assert os.environ["POLYROB_PROFILE"] == "scout"


def test_weak_tier_defers_to_explicit_env_and_warns_once(_isolated, monkeypatch, capsys):
    _mk_profile(_isolated, "sticky1")
    (_isolated / "active_profile").write_text("sticky1\n")
    monkeypatch.setenv("POLYROB_DATA_DIR", "/var/lib/polyrob")  # prod shape
    sel = resolve_active_profile()
    apply_profile_env(sel)
    apply_profile_env(sel)
    assert os.environ["POLYROB_DATA_DIR"] == "/var/lib/polyrob"  # never clobbered
    err = capsys.readouterr().err
    assert err.count("explicit environment wins") == 1  # warned exactly once


def test_project_dir_never_overwritten(_isolated, monkeypatch):
    home = _mk_profile(_isolated, "scout")
    monkeypatch.setenv("POLYROB_PROJECT_DIR", "/my/project")
    apply_profile_env(resolve_active_profile("scout"))
    assert os.environ["POLYROB_PROJECT_DIR"] == "/my/project"
    assert os.environ["POLYROB_HOME"] == str(home)


def test_apply_sets_project_dir_to_cwd_when_unset(_isolated):
    _mk_profile(_isolated, "scout")
    apply_profile_env(resolve_active_profile("scout"))
    assert os.environ["POLYROB_PROJECT_DIR"] == str(Path.cwd())


def test_apply_pins_profiles_root_before_moving_home(_isolated):
    _mk_profile(_isolated, "scout")
    apply_profile_env(resolve_active_profile("scout"))
    # In-profile `polyrob profile list` must still see the shared registry.
    assert profiles_root() == _isolated / "profiles"
    assert Path(os.environ["POLYROB_PROFILES_ROOT"]) == _isolated / "profiles"


def test_activate_legacy_mode_is_inert(_isolated):
    before = {k: os.environ.get(k) for k in
              ("POLYROB_HOME", "POLYROB_DATA_DIR", "POLYROB_PROJECT_DIR")}
    assert activate_profile() is None
    after = {k: os.environ.get(k) for k in before}
    assert after == before  # byte-identical legacy mode


def test_prod_env_shape_is_untouched(_isolated, monkeypatch):
    # The Hetzner box: explicit POLYROB_DATA_DIR, no flag/env/pin/sticky.
    monkeypatch.setenv("POLYROB_DATA_DIR", "/var/lib/polyrob")
    assert activate_profile() is None
    assert os.environ["POLYROB_DATA_DIR"] == "/var/lib/polyrob"
    assert "POLYROB_PROJECT_DIR" not in os.environ


# ── fallback warning (Hermes scar) ─────────────────────────────────────────


def test_fallback_warning_fires_once_without_resolution(_isolated, monkeypatch, capsys):
    _mk_profile(_isolated, "sticky1")
    (_isolated / "active_profile").write_text("sticky1\n")
    # Simulate a spawned process: sticky exists, but no POLYROB_HOME and no
    # resolution marker. (POLYROB_HOME is our test isolation var, so shift the
    # sticky lookup to the profiles-root pin instead.)
    monkeypatch.setenv("POLYROB_PROFILES_ROOT", str(_isolated / "profiles"))
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    warn_profile_fallback_once()
    warn_profile_fallback_once()
    err = capsys.readouterr().err
    assert err.count("wrong profile") == 1


def test_fallback_warning_suppressed_after_activation(_isolated, capsys):
    _mk_profile(_isolated, "sticky1")
    (_isolated / "active_profile").write_text("sticky1\n")
    activate_profile()
    warn_profile_fallback_once()
    assert "wrong profile" not in capsys.readouterr().err


# ── listing ─────────────────────────────────────────────────────────────────


def test_list_profiles_reads_metadata_fail_open(_isolated):
    a = _mk_profile(_isolated, "alpha")
    (a / "profile.yaml").write_text("display_name: Alpha\ndescription: A test bot\n")
    b = _mk_profile(_isolated, "beta")
    (b / "profile.yaml").write_text(":: not yaml ::")
    infos = list_profiles()
    assert [i.name for i in infos] == ["alpha", "beta"]
    assert infos[0].description == "A test bot"
    assert infos[1].description == ""
