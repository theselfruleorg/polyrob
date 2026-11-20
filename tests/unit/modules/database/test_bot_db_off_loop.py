"""bot.db statements run off the event loop (proposal 125 S18c, B-H2).

``DatabaseConnection`` ran each statement on the loop thread: a write that waited
on another process's lock (``busy_timeout`` 5 s) froze every session for that long.
Each statement and its auto-commit now run as one call on the connection's own
single thread.
"""
import asyncio
import sqlite3
import threading

import pytest

from modules.database.connection import DatabaseConnection


@pytest.mark.asyncio
async def test_a_contended_write_does_not_freeze_the_loop(tmp_path):
    path = tmp_path / "bot.db"
    db = DatabaseConnection(path)
    await db.connect()
    await db.execute("CREATE TABLE t (x INTEGER)")

    holder = sqlite3.connect(path, timeout=0.1, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    write = asyncio.create_task(db.execute("INSERT INTO t VALUES (1)"))
    await asyncio.sleep(0.5)
    try:
        assert ticks > 20, f"the loop froze: {ticks} ticks in 0.5 s"
        assert not write.done()
    finally:
        holder.rollback()
        holder.close()
    await asyncio.wait_for(write, 10)
    stop = True
    await t
    row = await db.fetch_one("SELECT COUNT(*) AS n FROM t")
    assert row["n"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_statements_run_on_one_thread_that_is_not_the_loop(tmp_path, monkeypatch):
    db = DatabaseConnection(tmp_path / "bot.db")
    seen = []
    real = sqlite3.Connection.execute

    class _Spy(sqlite3.Connection):
        def execute(self, *a, **k):
            seen.append(threading.get_ident())
            return real(self, *a, **k)

    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: real_connect(*a, factory=_Spy, **k))
    await db.connect()
    await db.execute("CREATE TABLE t (x INTEGER)")
    await db.fetch_all("SELECT * FROM t")
    assert seen and threading.get_ident() not in seen
    assert len(set(seen)) == 1
    await db.close()
