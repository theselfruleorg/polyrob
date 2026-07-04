"""`cli/update/migrate_guarded.py` — a DB migration that restores byte-identical on failure.

The scariest update failure is a migration dying half-way, leaving a wedged schema. This
wraps the migration in a pre-migrate snapshot and restores every DB if it throws, so a
failed migration is a no-op instead of a corrupt database.
"""
import sqlite3

import pytest

from cli.update.migrate_guarded import migrate_guarded


def _seed(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    c.executemany("INSERT INTO t DEFAULT VALUES", [() for _ in range(rows)])
    c.commit()
    c.close()


def _count(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        c.close()


def test_failed_migration_restores_db_byte_identical(tmp_path):
    home = tmp_path / "home"
    db = home / "memory.db"
    _seed(db, 10)

    def bad_migrate():
        # mutate then blow up mid-way
        _seed(db, 5)          # now 15
        raise RuntimeError("migration exploded")

    res = migrate_guarded(migrate=bad_migrate, db_paths=[db],
                          snapshots_root=tmp_path / "snaps", data_home=home,
                          from_version="0.4.2")
    assert res.ok is False
    assert isinstance(res.error, RuntimeError)
    assert _count(db) == 10          # rolled back — the 5 inserts are gone


def test_successful_migration_keeps_changes(tmp_path):
    home = tmp_path / "home"
    db = home / "memory.db"
    _seed(db, 10)

    def good_migrate():
        _seed(db, 5)          # 15

    res = migrate_guarded(migrate=good_migrate, db_paths=[db],
                          snapshots_root=tmp_path / "snaps", data_home=home,
                          from_version="0.4.2")
    assert res.ok is True
    assert res.error is None
    assert _count(db) == 15          # migration kept


def test_pre_migrate_snapshot_is_recorded(tmp_path):
    home = tmp_path / "home"
    db = home / "memory.db"
    _seed(db, 3)
    res = migrate_guarded(migrate=lambda: None, db_paths=[db],
                          snapshots_root=tmp_path / "snaps", data_home=home,
                          from_version="0.4.2")
    assert res.snapshot is not None and res.snapshot.complete
