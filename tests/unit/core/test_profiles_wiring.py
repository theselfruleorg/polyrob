"""W4 wiring: instance-id profile tier, cross-profile guard, init --profile."""
import os
from pathlib import Path

import pytest

from core.instance import resolve_instance_id
from core.profiles import cross_profile_access_allowed, foreign_profile_of_path


@pytest.fixture
def env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "profiles" / "scout").mkdir(parents=True)
    (home / "profiles" / "other").mkdir(parents=True)
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_PROFILES_ROOT", raising=False)
    monkeypatch.delenv("POLYROB_ALLOW_CROSS_PROFILE", raising=False)
    return home


# ── resolve_instance_id profile tier ────────────────────────────────────────


def test_instance_id_profile_tier():
    assert resolve_instance_id(env={"POLYROB_PROFILE": "scout"}) == "scout"


def test_explicit_instance_id_beats_profile():
    env = {"POLYROB_PROFILE": "scout", "POLYROB_INSTANCE_ID": "rob"}
    assert resolve_instance_id(env=env) == "rob"


def test_unsafe_profile_name_never_becomes_instance_id():
    assert resolve_instance_id(env={"POLYROB_PROFILE": "../x"}) == "polyrob"


# ── cross-profile path guard ────────────────────────────────────────────────


def test_foreign_profile_detected(env, monkeypatch):
    monkeypatch.setenv("POLYROB_PROFILE", "scout")
    assert foreign_profile_of_path(env / "profiles" / "other" / "data" / "memory.db") == "other"
    assert foreign_profile_of_path(env / "profiles" / "scout" / ".env") is None
    assert foreign_profile_of_path("/somewhere/else") is None


def test_legacy_mode_treats_every_profile_as_foreign(env):
    assert foreign_profile_of_path(env / "profiles" / "scout" / ".env") == "scout"


def test_cross_profile_bypass_flag(monkeypatch):
    assert not cross_profile_access_allowed()
    monkeypatch.setenv("POLYROB_ALLOW_CROSS_PROFILE", "1")
    assert cross_profile_access_allowed()


# ── init --profile writes identity to the profile, keys to the global home ──


def test_init_profile_splits_identity_from_global(env, monkeypatch, tmp_path):
    from click.testing import CliRunner

    from cli.commands.init import init_cmd
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(init_cmd, [
        "--no-prompt", "--skip-keys", "--profile", "scout",
        "--instance-id", "scout", "--owner", "boss",
        "--default-model", "claude-fable-5",
    ], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    profile_env = (env / "profiles" / "scout" / ".env").read_text()
    assert "POLYROB_INSTANCE_ID=scout" in profile_env
    assert "POLYROB_OWNER_USER_ID=boss" in profile_env
    global_env = (env / ".env").read_text() if (env / ".env").exists() else ""
    assert "POLYROB_INSTANCE_ID" not in global_env  # identity never global
    assert "DEFAULT_MODEL=claude-fable-5" in global_env  # model stays global


def test_init_profile_creates_missing_profile(env, monkeypatch, tmp_path):
    from click.testing import CliRunner

    from cli.commands.init import init_cmd
    monkeypatch.setenv("POLYROB_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(init_cmd, [
        "--no-prompt", "--skip-keys", "--profile", "fresh",
    ], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    assert (env / "profiles" / "fresh" / "profile.yaml").is_file()
