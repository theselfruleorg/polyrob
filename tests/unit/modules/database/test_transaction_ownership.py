"""H8 — shared-connection transaction ownership.

`DatabaseConnection` is ONE sync sqlite3 connection behind a per-statement
asyncio.Lock. Before the fix, while a transaction was open EVERY coroutine's
`execute()` skipped auto-commit and silently JOINED that transaction — so a
concurrent write from a DIFFERENT task became part of the uncommitted span and
could be destroyed by the owner's ROLLBACK (the H8 fund-loss bug: an
already-acknowledged settle UPDATE undone by the watcher's rollback).

The fix makes a transaction span OWN the connection: `begin_transaction` records
the owner task; an `execute()` from another task WAITS for the transaction to
finish instead of joining it; the owner's own statements proceed.
"""
import asyncio

import pytest

from modules.database.connection import DatabaseConnection


async def _fresh(tmp_path):
    db = DatabaseConnection(tmp_path / "txn.db")
    await db.connect()
    await db.execute("CREATE TABLE t (id TEXT PRIMARY KEY, v TEXT)")
    return db


@pytest.mark.asyncio
async def test_concurrent_write_does_not_join_transaction(tmp_path):
    """The H8 scenario: a different task's write during an open transaction must
    WAIT, not join — so it SURVIVES the owner's ROLLBACK. Pre-fix, row 'B' would
    have joined the owner's transaction and been rolled back with 'A' (result
    []); with the fix 'B' waits then commits independently (result ['B'])."""
    db = await _fresh(tmp_path)
    try:
        b_may_start = asyncio.Event()
        b_did_execute = asyncio.Event()

        async def owner():
            await db.begin_transaction()
            await db.execute("INSERT INTO t (id, v) VALUES ('A', 'owner')")
            b_may_start.set()
            # Give the writer task a chance to attempt (and block on) its execute.
            await asyncio.sleep(0.05)
            # The writer must NOT have completed — it is waiting on our txn.
            assert not b_did_execute.is_set()
            await db.rollback()  # undoes 'A' only

        async def writer():
            await b_may_start.wait()
            await db.execute("INSERT INTO t (id, v) VALUES ('B', 'writer')")
            b_did_execute.set()

        await asyncio.gather(owner(), writer())

        rows = await db.fetch_all("SELECT id FROM t ORDER BY id")
        assert [r["id"] for r in rows] == ["B"]  # 'A' rolled back, 'B' survived
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_owner_statements_proceed_within_span(tmp_path):
    """The owner task runs multiple statements inside its own transaction and
    commits — none of them wait on themselves (same-task ownership)."""
    db = await _fresh(tmp_path)
    try:
        await db.begin_transaction()
        await db.execute("INSERT INTO t (id, v) VALUES ('x', '1')")
        await db.execute("INSERT INTO t (id, v) VALUES ('y', '2')")
        await db.execute("UPDATE t SET v = '3' WHERE id = 'x'")
        await db.commit()
        rows = await db.fetch_all("SELECT id, v FROM t ORDER BY id")
        assert {r["id"]: r["v"] for r in rows} == {"x": "3", "y": "2"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_waiter_proceeds_after_commit(tmp_path):
    """A different task's write during the span is applied (committed) once the
    owner COMMITS — the transaction's own rows and the waiter's row all land."""
    db = await _fresh(tmp_path)
    try:
        started = asyncio.Event()

        async def owner():
            await db.begin_transaction()
            await db.execute("INSERT INTO t (id, v) VALUES ('A', 'owner')")
            started.set()
            await asyncio.sleep(0.05)
            await db.commit()

        async def writer():
            await started.wait()
            await db.execute("INSERT INTO t (id, v) VALUES ('B', 'writer')")

        await asyncio.gather(owner(), writer())
        rows = await db.fetch_all("SELECT id FROM t ORDER BY id")
        assert [r["id"] for r in rows] == ["A", "B"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_abandoned_owner_does_not_deadlock(tmp_path):
    """If the owner task ends WITHOUT commit/rollback (unhandled cancellation
    between begin and its first statement), a later different-task execute must
    NOT wait forever — the abandoned span is cleared and it proceeds."""
    db = await _fresh(tmp_path)
    try:
        async def bad_owner():
            await db.begin_transaction()
            # Never commit/rollback — simulate an abandoned span by cancelling.
            raise asyncio.CancelledError()

        task = asyncio.ensure_future(bad_owner())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()

        # A different task's write must still go through (bounded, no hang).
        await asyncio.wait_for(
            db.execute("INSERT INTO t (id, v) VALUES ('C', 'after')"), timeout=2.0)
        rows = await db.fetch_all("SELECT id FROM t")
        assert [r["id"] for r in rows] == ["C"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_abandoned_owner_partial_write_is_rolled_back(tmp_path):
    """H8 escape-hatch fix: if the dead owner left a PARTIAL write inside its
    abandoned transaction (e.g. a balance UPDATE without its paired ledger
    INSERT), the escape hatch must roll back that sqlite-level span BEFORE
    clearing the Python-side flags — otherwise the next non-owner execute()
    auto-commits the ENTIRE pending sqlite transaction, silently landing the
    dead task's partial row as a side effect of its own unrelated write
    (money fails OPEN)."""
    db = await _fresh(tmp_path)
    try:
        async def bad_owner():
            await db.begin_transaction()
            # Partial write: the owner got this far before being cancelled,
            # simulating e.g. a balance UPDATE without its paired ledger
            # INSERT.
            await db.execute(
                "INSERT INTO t (id, v) VALUES ('PARTIAL', 'dead-owner')")
            raise asyncio.CancelledError()

        task = asyncio.ensure_future(bad_owner())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()

        # A different task's write must still go through (bounded, no hang).
        await asyncio.wait_for(
            db.execute("INSERT INTO t (id, v) VALUES ('C', 'after')"),
            timeout=2.0)

        rows = await db.fetch_all("SELECT id FROM t ORDER BY id")
        # The dead owner's PARTIAL row must be discarded — only the later
        # writer's row survives.
        assert [r["id"] for r in rows] == ["C"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_writers_outside_transaction_both_commit(tmp_path):
    """No transaction open: concurrent writers from different tasks both
    auto-commit (the gate only engages while a span is open)."""
    db = await _fresh(tmp_path)
    try:
        await asyncio.gather(
            db.execute("INSERT INTO t (id, v) VALUES ('p', '1')"),
            db.execute("INSERT INTO t (id, v) VALUES ('q', '2')"),
        )
        rows = await db.fetch_all("SELECT id FROM t ORDER BY id")
        assert [r["id"] for r in rows] == ["p", "q"]
    finally:
        await db.close()
