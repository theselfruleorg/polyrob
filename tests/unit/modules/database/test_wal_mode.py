"""bot.db runs in WAL: the agent, the console and the API share the file, and
without it one writer blocked every reader for up to the busy timeout."""
import sqlite3

import pytest

from modules.database.connection import DatabaseConnection


@pytest.mark.asyncio
async def test_connect_switches_the_file_to_wal_and_it_stays(tmp_path):
    path = tmp_path / "bot.db"
    db = DatabaseConnection(path)
    await db.connect()
    try:
        await db.execute("CREATE TABLE t (x INTEGER)")
        await db.execute("INSERT INTO t VALUES (1)")
    finally:
        await db.close()
    con = sqlite3.connect(path)          # another process: the mode is in the file
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        con.close()


@pytest.mark.asyncio
async def test_a_reader_is_not_blocked_by_an_open_write(tmp_path):
    path = tmp_path / "bot.db"
    db = DatabaseConnection(path)
    await db.connect()
    try:
        await db.execute("CREATE TABLE t (x INTEGER)")
        # EXCLUSIVE + a write: in a rollback-journal mode this blocks every
        # reader (a RESERVED lock from BEGIN IMMEDIATE would not); in WAL the
        # reader still sees the last commit.
        writer = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("INSERT INTO t VALUES (2)")
            assert await db.fetch_all("SELECT x FROM t") == []   # reads the last commit
        finally:
            writer.rollback()
            writer.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_refused_switch_keeps_the_connection_working(tmp_path, monkeypatch):
    db = DatabaseConnection(tmp_path / "bot.db")

    real = DatabaseConnection._enable_wal

    def _wrapped(self):
        class _C:
            def __init__(s, c): s._c = c
            def execute(s, sql, *a):
                if "journal_mode" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return s._c.execute(sql, *a)
        conn, self.connection = self.connection, _C(self.connection)
        try:
            real(self)
        finally:
            self.connection = conn

    monkeypatch.setattr(DatabaseConnection, "_enable_wal", _wrapped)
    await db.connect()
    try:
        await db.execute("CREATE TABLE t (x INTEGER)")
        assert await db.fetch_all("SELECT x FROM t") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_wal_file_has_the_shared_size_limit(tmp_path):
    """Without a limit the -wal file stays at its peak size after a checkpoint."""
    from core.sqlite_util import _WAL_SIZE_LIMIT_BYTES
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        limit = db.connection.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert limit == _WAL_SIZE_LIMIT_BYTES
    finally:
        await db.close()


def _mode(p):
    import os
    import stat
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.asyncio
async def test_a_new_bot_db_and_its_wal_files_are_born_without_other_access(tmp_path):
    """A bare sqlite3.connect births bot.db 0644 under umask 022, and -wal/-shm
    copy that mode. The shared birth rule gives group access, never other."""
    import os
    old = os.umask(0o022)
    try:
        path = tmp_path / "bot.db"
        db = DatabaseConnection(path)
        await db.connect()
        try:
            await db.execute("CREATE TABLE t (x INTEGER)")
            for p in (path, tmp_path / "bot.db-wal", tmp_path / "bot.db-shm"):
                assert _mode(p) == 0o660, (p, oct(_mode(p)))
        finally:
            await db.close()
    finally:
        os.umask(old)


@pytest.mark.asyncio
async def test_a_new_bot_db_in_a_setgid_home_keeps_the_shared_group(tmp_path):
    """The deployed layout: a setgid 2770 data home shared by several service
    users. The file takes the directory's group and is group read-write."""
    import os
    home = tmp_path / "data"
    home.mkdir()
    os.chmod(home, 0o2770)
    path = home / "bot.db"
    db = DatabaseConnection(path)
    await db.connect()
    try:
        await db.execute("CREATE TABLE t (x INTEGER)")
    finally:
        await db.close()
    assert _mode(path) == 0o660
    assert os.stat(path).st_gid == os.stat(home).st_gid


@pytest.mark.asyncio
async def test_an_existing_bot_db_keeps_its_mode(tmp_path):
    import os
    path = tmp_path / "bot.db"
    sqlite3.connect(path).close()
    os.chmod(path, 0o644)
    db = DatabaseConnection(path)
    await db.connect()
    await db.close()
    assert _mode(path) == 0o644


@pytest.mark.asyncio
async def test_a_span_that_reads_then_writes_survives_another_process_commit(tmp_path):
    """Under WAL a DEFERRED span that read a snapshot and then writes fails at once
    (BUSY_SNAPSHOT, no busy wait) when another process committed in between. The
    span takes the write lock at BEGIN, so the other writer waits instead."""
    path = tmp_path / "bot.db"
    db = DatabaseConnection(path)
    await db.connect()
    try:
        await db.execute("CREATE TABLE t (x INTEGER)")
        await db.execute("INSERT INTO t VALUES (1)")
        await db.begin_transaction()
        assert await db.fetch_all("SELECT x FROM t") == [{"x": 1}]
        other = sqlite3.connect(path, timeout=0.1, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError):
                other.execute("INSERT INTO t VALUES (2)")   # waits on the span
        finally:
            other.close()
        await db.execute("UPDATE t SET x = 3")
        await db.commit()
        assert await db.fetch_all("SELECT x FROM t") == [{"x": 3}]
    finally:
        await db.close()
