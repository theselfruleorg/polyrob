"""C3/C2: schema migrations run idempotently at boot; a real change snapshots first.

Covers the boot-migration contract in isolation with a minimal async sqlite adapter and
synthetic migration files, so it needs no container/bot:
  1. fresh DB -> shipped migrations are STAMPED at HEAD, not executed (no side effects);
  2. a pending future migration EXECUTES, records to the SSOT, and fires the C2
     snapshot hook exactly once;
  3. booting twice is a no-op (no double-apply / double-insert);
  4. a failing migration is fail-open (never raises; leaves the DB usable);
  5. the single-flight lock makes a concurrent boot a no-op.
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from migrations.boot import apply_migrations_at_boot


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

    def columns(self, table):
        return [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _write_migration(dir_: Path, fname: str, version: str, body: str):
    (dir_ / fname).write_text(
        f'VERSION = "{version}"\n'
        f'DESCRIPTION = "test {version}"\n'
        "async def upgrade(db, db_manager):\n"
        f"{body}\n"
    )


@pytest.fixture
def versions_dir(tmp_path):
    d = tmp_path / "versions"
    d.mkdir()
    return d


@pytest.fixture
def db(tmp_path):
    return _FakeDB(tmp_path / "bot.db")


def test_fresh_db_baselines_at_head_without_executing(db, versions_dir):
    # A shipped migration whose side effect (create table 'sentinel') would be visible
    # IF it ran. On a fresh DB it must be stamped, not executed.
    _write_migration(versions_dir, "v1_0_0_x.py", "1.0.0",
                     '    await db.execute("CREATE TABLE sentinel (id INTEGER)")')
    _write_migration(versions_dir, "v1_1_0_y.py", "1.1.0",
                     '    await db.execute("CREATE TABLE sentinel2 (id INTEGER)")')

    summary = asyncio.run(apply_migrations_at_boot(db, versions_dir=versions_dir))

    assert summary["baselined"] is True
    assert set(summary["applied"]) == {"1.0.0", "1.1.0"}
    # stamped, NOT executed -> the migration side-effect tables must NOT exist
    assert not db.table_exists("sentinel")
    assert not db.table_exists("sentinel2")
    # both versions recorded in the SSOT
    rows = asyncio.run(db.fetch_all("SELECT version FROM schema_versions"))
    assert {r["version"] for r in rows} == {"1.0.0", "1.1.0"}


def test_pending_migration_executes_and_snapshots_once(db, versions_dir):
    # Pre-existing table a future migration will ALTER.
    asyncio.run(db.execute("CREATE TABLE widgets (id INTEGER)"))
    # Baseline present (v1.0.0), plus a genuinely-pending future migration.
    _write_migration(versions_dir, "v1_0_0_x.py", "1.0.0", "    pass")
    _write_migration(versions_dir, "v1_5_0_add_color.py", "1.5.0",
                     '    await db.execute("ALTER TABLE widgets ADD COLUMN color TEXT")')

    # Simulate a DB already baselined at 1.0.0 (so 1.5.0 is genuinely pending).
    asyncio.run(_prestamp(db, ["1.0.0"]))

    calls = []
    summary = asyncio.run(apply_migrations_at_boot(
        db, versions_dir=versions_dir, on_before_change=lambda: calls.append(1)))

    assert summary["baselined"] is False
    assert summary["applied"] == ["1.5.0"]
    assert "color" in db.columns("widgets")          # migration actually ran
    assert calls == [1]                              # snapshot hook fired exactly once
    applied = {r["version"] for r in asyncio.run(db.fetch_all("SELECT version FROM schema_versions"))}
    assert "1.5.0" in applied


def test_second_boot_is_noop(db, versions_dir):
    asyncio.run(db.execute("CREATE TABLE widgets (id INTEGER)"))
    _write_migration(versions_dir, "v1_0_0_x.py", "1.0.0", "    pass")
    _write_migration(versions_dir, "v1_5_0_add_color.py", "1.5.0",
                     '    await db.execute("ALTER TABLE widgets ADD COLUMN color TEXT")')
    asyncio.run(_prestamp(db, ["1.0.0"]))

    first = asyncio.run(apply_migrations_at_boot(db, versions_dir=versions_dir))
    assert first["applied"] == ["1.5.0"]

    calls = []
    second = asyncio.run(apply_migrations_at_boot(
        db, versions_dir=versions_dir, on_before_change=lambda: calls.append(1)))
    assert second["applied"] == []          # nothing left to do
    assert second["pending"] == []
    assert calls == []                      # no snapshot on a no-op boot
    assert second["error"] is None


def test_failing_migration_is_fail_open(db, versions_dir):
    _write_migration(versions_dir, "v1_0_0_x.py", "1.0.0", "    pass")
    _write_migration(versions_dir, "v1_5_0_boom.py", "1.5.0",
                     '    await db.execute("ALTER TABLE nonexistent ADD COLUMN c TEXT")')
    asyncio.run(_prestamp(db, ["1.0.0"]))

    # Must NOT raise — returns a summary carrying the error instead.
    summary = asyncio.run(apply_migrations_at_boot(db, versions_dir=versions_dir))
    assert summary["error"] is not None
    assert "1.5.0" not in summary["applied"]


def test_lock_held_skips(db, versions_dir, tmp_path):
    _write_migration(versions_dir, "v1_0_0_x.py", "1.0.0", "    pass")
    lock_path = tmp_path / "migrate.lock"
    import fcntl
    held = open(lock_path, "w")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    try:
        summary = asyncio.run(apply_migrations_at_boot(
            db, versions_dir=versions_dir, lock_path=lock_path))
        assert summary["skipped_lock"] is True
        assert summary["applied"] == []
        # nothing recorded because the lock was not acquired
        assert not db.table_exists("schema_versions") or \
            asyncio.run(db.fetch_all("SELECT * FROM schema_versions")) == []
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


async def _prestamp(db, versions):
    """Record given versions as applied (simulate a DB already baselined)."""
    from migrations.version_manager import DatabaseVersionManager
    vm = DatabaseVersionManager(db)
    await vm.initialize()
    for v in versions:
        await vm.record_migration(v, f"prestamp {v}")
