"""Tests for core/home_migration.py::migrate_rob_home_once.

Monkeypatch ``$HOME`` to a tmp_path so the copy is fully isolated.
"""

import shutil

import pytest

from core.home_migration import migrate_rob_home_once, _MARKER_NAME


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("POLYROB_HOME", raising=False)
    return tmp_path


def _seed_legacy(home):
    legacy = home / ".rob"
    legacy.mkdir()
    (legacy / ".env").write_text("ANTHROPIC_API_KEY=sk-test\n")
    (legacy / "cli.json").write_text("{}\n")
    return legacy


def test_migrates_when_polyrob_absent(fake_home):
    _seed_legacy(fake_home)
    migrate_rob_home_once()
    new_home = fake_home / ".polyrob"
    assert (new_home / ".env").read_text() == "ANTHROPIC_API_KEY=sk-test\n"
    assert (new_home / "cli.json").exists()
    assert (new_home / _MARKER_NAME).exists()
    # non-destructive: legacy left intact
    assert (fake_home / ".rob" / ".env").exists()


def test_idempotent_second_call_is_noop(fake_home):
    _seed_legacy(fake_home)
    migrate_rob_home_once()
    new_home = fake_home / ".polyrob"
    # Mutate the new home + legacy, then call again — must NOT re-copy/overwrite.
    (new_home / ".env").write_text("CHANGED\n")
    (fake_home / ".rob" / ".env").write_text("LEGACY-CHANGED\n")
    migrate_rob_home_once()
    assert (new_home / ".env").read_text() == "CHANGED\n"  # no re-copy
    assert (fake_home / ".rob" / ".env").read_text() == "LEGACY-CHANGED\n"  # untouched


def test_noop_when_neither_dir_exists(fake_home):
    # No ~/.rob, no ~/.polyrob — no crash, nothing created.
    migrate_rob_home_once()
    assert not (fake_home / ".polyrob").exists()


def test_fail_open_on_copy_error(fake_home, monkeypatch):
    _seed_legacy(fake_home)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", _boom)
    # Must not raise (fail-open) ...
    migrate_rob_home_once()
    # ... and must leave a usable ~/.polyrob behind.
    assert (fake_home / ".polyrob").exists()
