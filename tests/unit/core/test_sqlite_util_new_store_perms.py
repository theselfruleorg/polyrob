"""A store CREATED between deploys must be writable by the shared group from birth.

Prod 2026-09-20 03:51Z: 057's new `verdicts.db` was born at 03:31:56 — thirty seconds
AFTER the deploy's data-ownership pass (`chmod g+rw`) had run — and SQLite creates a
db file 0644 whatever the unit's UMask=0002 says. Twenty minutes later the register
degraded with "attempt to write a readonly database" and the SMTP backoff fell back to
process-local memory. Every other store is 0660 only because a later deploy repaired
it. `init_schema` now applies the same rule at creation, so a store never waits for the
next deploy to become shared.
"""
import os
import stat

import pytest

from core import sqlite_util


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_a_new_store_is_group_writable(tmp_path):
    old = os.umask(0o022)
    try:
        p = tmp_path / "new.db"
        sqlite_util.init_schema(str(p), "CREATE TABLE IF NOT EXISTS t (x)")
        assert _mode(p) & 0o060 == 0o060, f"group rw expected, got {oct(_mode(p))}"
        assert _mode(p) & 0o002 == 0, "never world-writable"
    finally:
        os.umask(old)


def test_an_existing_store_mode_is_left_alone(tmp_path):
    p = tmp_path / "old.db"
    sqlite_util.init_schema(str(p), "CREATE TABLE IF NOT EXISTS t (x)")
    os.chmod(p, 0o600)  # an operator's deliberate tightening
    sqlite_util.init_schema(str(p), "CREATE TABLE IF NOT EXISTS t (x)")
    assert _mode(p) == 0o600


def test_chmod_failure_is_not_fatal(tmp_path, monkeypatch):
    p = tmp_path / "new.db"

    def _boom(*a, **k):
        raise PermissionError("no chmod for you")

    monkeypatch.setattr(os, "chmod", _boom)
    sqlite_util.init_schema(str(p), "CREATE TABLE IF NOT EXISTS t (x)")
    assert p.exists()
