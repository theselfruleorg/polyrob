"""Tests for core/home_migration.py (home + identity-instance migrations).

Monkeypatch ``$HOME`` to a tmp_path so the copy is fully isolated.
"""

import shutil

import pytest

from core.home_migration import (
    _IDENTITY_MARKER_NAME,
    _MARKER_NAME,
    migrate_identity_instance_once,
    migrate_rob_home_once,
)


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


# ── W1: identity/rob -> identity/polyrob (DEFAULT_INSTANCE_ID rename) ───────


@pytest.fixture
def identity_home(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    # A leaked POLYROB_PROFILE (resolve_instance_id tier 3) would make the
    # migration see a non-default instance and skip — keep this order-robust.
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    legacy = tmp_path / "identity" / "rob" / "user_owner"
    legacy.mkdir(parents=True)
    (legacy / "self.md").write_text("I am the evolving self doc.\n")
    return tmp_path


def test_identity_migration_copies_and_keeps_source(identity_home):
    migrate_identity_instance_once(identity_home)
    new_doc = identity_home / "identity" / "polyrob" / "user_owner" / "self.md"
    assert new_doc.read_text() == "I am the evolving self doc.\n"
    assert (identity_home / "identity" / _IDENTITY_MARKER_NAME).exists()
    # copy-not-move: the legacy tree stays intact
    assert (identity_home / "identity" / "rob" / "user_owner" / "self.md").exists()


def test_identity_migration_is_idempotent(identity_home):
    migrate_identity_instance_once(identity_home)
    new_doc = identity_home / "identity" / "polyrob" / "user_owner" / "self.md"
    new_doc.write_text("EVOLVED\n")
    migrate_identity_instance_once(identity_home)
    assert new_doc.read_text() == "EVOLVED\n"  # no re-copy/overwrite


def test_identity_migration_skips_explicit_instance_id(identity_home, monkeypatch):
    # Prod pins POLYROB_INSTANCE_ID=rob — its tree keeps working, no copy needed.
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    migrate_identity_instance_once(identity_home)
    assert not (identity_home / "identity" / "polyrob").exists()


def test_identity_migration_noop_without_legacy_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    migrate_identity_instance_once(tmp_path)
    assert not (tmp_path / "identity").exists()


def test_identity_migration_fail_open_on_copy_error(identity_home, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", _boom)
    migrate_identity_instance_once(identity_home)  # must not raise
    assert (identity_home / "identity" / "rob").exists()  # source untouched
