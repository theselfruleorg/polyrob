"""Regression (P0): the v1.0.0 baseline migration self-inserted into
`schema_version` (SINGULAR) — a table that is never created — so `migrate upgrade`
crashed on a fresh DB with "no such table". Recording is the runner's job
(DatabaseVersionManager writes `schema_versions`, plural). The migration must run
cleanly and verify() must query the canonical plural table.
"""
import sqlite3

import pytest

from migrations.version_manager import DatabaseVersionManager
from migrations.versions.v1_0_0_baseline import upgrade, verify, VERSION


class _FakeDB:
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


class _NoopTable:
    async def create_table(self):
        pass

    async def create_tables(self):
        pass


class _FakeMgr:
    """db_manager with no registered tables: every guarded create step is skipped,
    so upgrade() reaches exactly the version-recording region under test."""
    tables = {}
    user_profiles = _NoopTable()


@pytest.mark.asyncio
async def test_baseline_upgrade_does_not_crash_and_records_to_plural_table(tmp_path):
    db = _FakeDB(tmp_path / "bot.db")
    vm = DatabaseVersionManager(db)
    await vm.initialize()  # creates the canonical `schema_versions` table

    # BEFORE the fix this raised sqlite3.OperationalError: no such table: schema_version
    ok = await upgrade(db, _FakeMgr())
    assert ok is True

    # The runner records the version into schema_versions (plural).
    await vm.record_migration(version=VERSION, description="baseline", execution_time_ms=0)
    assert await vm.is_version_applied(VERSION)

    # verify() must query the plural table without raising (it previously hit the
    # nonexistent singular table). It returns a bool; the table checks fail here
    # because the fake db_manager creates no real tables — we only assert it ran.
    assert isinstance(await verify(db, _FakeMgr()), bool)


@pytest.mark.asyncio
async def test_singular_schema_version_table_is_never_written(tmp_path):
    db = _FakeDB(tmp_path / "bot.db")
    vm = DatabaseVersionManager(db)
    await vm.initialize()
    await upgrade(db, _FakeMgr())
    # The buggy singular table must not have been created by the migration.
    row = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    assert row is None, "migration must not create/write the singular schema_version table"
