"""The off-loop seam (proposal 125 S15): a blocking store call from async code.

``off_loop(obj)`` runs each method of a sync store in the store pool; ``@offloaded``
turns a sync method body into an awaitable one; ``shielded`` lets a terminal write
finish after a cancel and then re-raises the cancel. ``write_txn`` is the one
``BEGIN IMMEDIATE`` transaction.
"""
import asyncio
import contextvars
import inspect
import sqlite3
import threading
import time

import pytest

from core.async_bridge import off_loop, offloaded, shielded
from core.sqlite_util import write_txn

_VAR = contextvars.ContextVar("_VAR", default="unset")


class _Store:
    limit = 7

    def where(self, x):
        return threading.get_ident(), _VAR.get(), x

    def slow_write(self, sink, delay):
        time.sleep(delay)
        sink.append("written")
        return "ok"


class _Async:
    @offloaded
    def where(self, x):
        return threading.get_ident(), _VAR.get(), x


@pytest.mark.asyncio
async def test_off_loop_runs_the_call_off_the_loop_thread():
    loop_thread = threading.get_ident()
    tid, _, x = await off_loop(_Store()).where(5)
    assert x == 5
    assert tid != loop_thread


@pytest.mark.asyncio
async def test_off_loop_propagates_contextvars():
    _VAR.set("tenant-a")
    _, seen, _ = await off_loop(_Store()).where(1)
    assert seen == "tenant-a"


@pytest.mark.asyncio
async def test_off_loop_passes_a_plain_attribute_through():
    assert off_loop(_Store()).limit == 7


@pytest.mark.asyncio
async def test_offloaded_is_a_coroutine_function_and_runs_off_the_loop():
    assert inspect.iscoroutinefunction(_Async.where)
    _VAR.set("tenant-b")
    tid, seen, x = await _Async().where(3)
    assert tid != threading.get_ident()
    assert seen == "tenant-b" and x == 3


@pytest.mark.asyncio
async def test_a_shielded_write_completes_after_a_cancel():
    sink = []
    after = []

    async def terminal():
        await shielded(off_loop(_Store()).slow_write(sink, 0.2))
        after.append("not reached")

    task = asyncio.create_task(terminal())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The cancel waited for the write: it is done when the task ends.
    assert sink == ["written"]
    assert after == []


@pytest.mark.asyncio
async def test_shielded_returns_the_result_without_a_cancel():
    assert await shielded(off_loop(_Store()).slow_write([], 0)) == "ok"


def test_write_txn_commits_on_success(tmp_path):
    db = str(tmp_path / "t.db")
    with write_txn(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_write_txn_rolls_back_on_error(tmp_path):
    db = str(tmp_path / "t.db")
    with write_txn(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(RuntimeError):
        with write_txn(db) as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("abort")
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_write_txn_waits_for_a_held_lock_then_raises_past_the_deadline(tmp_path):
    db = str(tmp_path / "t.db")
    with write_txn(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    holder = sqlite3.connect(db)
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            with write_txn(db, deadline_sec=0.3):
                pass
        assert time.monotonic() - t0 < 3.0
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.asyncio
async def test_a_shielding_proxy_finishes_the_write_after_a_cancel():
    sink = []

    async def terminal():
        await off_loop(_Store(), shield=True).slow_write(sink, 0.2)

    task = asyncio.create_task(terminal())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink == ["written"]
