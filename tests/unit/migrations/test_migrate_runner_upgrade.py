"""U1 (2026-07-14 review): the CLI migration runner must survive self-recording migrations.

Several shipped migrations (v1_1_0, v1_5_0, v1_6_0, v1_7_0, ...) self-insert their version
via ``INSERT OR REPLACE INTO schema_versions``. The runner used to unconditionally
``record_migration`` afterwards — a plain INSERT into a UNIQUE column → IntegrityError →
exit 1, which made ``polyrob update --apply`` deterministically roll back any release
containing a self-recording migration. These tests drive the exact upgrade loop
``run_migrations('upgrade')`` executes (extracted as ``apply_pending_migrations``) against
a temp DB with a self-recording migration file.
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from migrations.migrate import apply_pending_migrations
from migrations.version_manager import DatabaseVersionManager


class _FakeDB:
    """Async facade over sqlite matching DatabaseVersionManager's db interface."""

    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row

    async def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    async def fetch_one(self, sql, params=()):
        return self._conn.execute(sql, params).fetchone()

    async def fetch_all(self, sql, params=()):
        return self._conn.execute(sql, params).fetchall()

    def table_exists(self, name):
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None


SELF_RECORDING_BODY = '''
VERSION = "9.9.0"
DESCRIPTION = "test self-recording migration"

async def upgrade(db, db_manager):
    await db.execute("CREATE TABLE IF NOT EXISTS u1_sentinel (id INTEGER)")
    # Mirrors v1_1_0/v1_5_0/v1_6_0/v1_7_0: the migration records itself.
    await db.execute(
        "INSERT OR REPLACE INTO schema_versions (version, description, applied_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)", (VERSION, DESCRIPTION))
'''

PLAIN_BODY = '''
VERSION = "9.8.0"
DESCRIPTION = "test plain migration"

async def upgrade(db, db_manager):
    await db.execute("CREATE TABLE IF NOT EXISTS u1_plain (id INTEGER)")
'''


@pytest.fixture
def versions_dir(tmp_path):
    d = tmp_path / "versions"
    d.mkdir()
    return d


@pytest.fixture
def db(tmp_path):
    return _FakeDB(tmp_path / "bot.db")


def _version_mgr(db) -> DatabaseVersionManager:
    vm = DatabaseVersionManager(db)
    asyncio.run(vm.initialize())
    return vm


def test_self_recording_migration_applies_and_records_once(db, versions_dir):
    (versions_dir / "v9_9_0_selfrec.py").write_text(SELF_RECORDING_BODY)
    vm = _version_mgr(db)

    applied = asyncio.run(apply_pending_migrations(db, None, vm, versions_dir))

    assert applied == ["9.9.0"]
    assert db.table_exists("u1_sentinel")
    rows = asyncio.run(db.fetch_all(
        "SELECT version FROM schema_versions WHERE version = ?", ("9.9.0",)))
    assert len(rows) == 1  # recorded exactly once, no IntegrityError


def test_second_run_is_noop(db, versions_dir):
    (versions_dir / "v9_9_0_selfrec.py").write_text(SELF_RECORDING_BODY)
    vm = _version_mgr(db)

    first = asyncio.run(apply_pending_migrations(db, None, vm, versions_dir))
    assert first == ["9.9.0"]

    second = asyncio.run(apply_pending_migrations(db, None, vm, versions_dir))
    assert second == []  # already applied → nothing to do


def test_plain_migration_still_recorded_by_runner(db, versions_dir):
    (versions_dir / "v9_8_0_plain.py").write_text(PLAIN_BODY)
    vm = _version_mgr(db)

    applied = asyncio.run(apply_pending_migrations(db, None, vm, versions_dir))

    assert applied == ["9.8.0"]
    assert db.table_exists("u1_plain")
    assert asyncio.run(vm.is_version_applied("9.8.0"))


def test_mixed_pending_set_applies_in_order(db, versions_dir):
    (versions_dir / "v9_8_0_plain.py").write_text(PLAIN_BODY)
    (versions_dir / "v9_9_0_selfrec.py").write_text(SELF_RECORDING_BODY)
    vm = _version_mgr(db)

    applied = asyncio.run(apply_pending_migrations(db, None, vm, versions_dir))

    assert applied == ["9.8.0", "9.9.0"]
    for v in ("9.8.0", "9.9.0"):
        rows = asyncio.run(db.fetch_all(
            "SELECT version FROM schema_versions WHERE version = ?", (v,)))
        assert len(rows) == 1
