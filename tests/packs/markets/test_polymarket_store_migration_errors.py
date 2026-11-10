"""The Polymarket store's additive columns: a concurrent writer's "duplicate
column" is the one error ignored; any other failure is logged, never silent."""
import logging
import sqlite3

import pytest

pytest.importorskip("polyrob_markets")

from polyrob_markets.polymarket.store import PolymarketDBHandler  # noqa: E402

from modules.database.connection import DatabaseConnection  # noqa: E402


async def _db(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    return db


@pytest.mark.asyncio
async def test_an_unreadable_column_list_is_logged_and_duplicates_stay_quiet(
        tmp_path, monkeypatch, caplog):
    db = await _db(tmp_path)
    try:
        await PolymarketDBHandler(db).ensure_tables()       # every column exists
        real = db.fetch_all

        async def _fetch_all(q, *a, **k):
            if "table_info(polymarket_credentials)" in q:
                raise sqlite3.OperationalError("database is locked")
            return await real(q, *a, **k)

        monkeypatch.setattr(db, "fetch_all", _fetch_all)
        with caplog.at_level(logging.DEBUG, logger="database.polymarket"):
            await PolymarketDBHandler(db).ensure_tables()
        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
                  and r.name == "database.polymarket"]
        assert any("database is locked" in m for m in warned), warned
        assert not any("duplicate column" in m for m in warned), warned
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_failed_add_column_is_logged(tmp_path, monkeypatch, caplog):
    db = await _db(tmp_path)
    try:
        await db.execute("CREATE TABLE polymarket_credentials (user_id TEXT PRIMARY KEY)")
        real = db.execute

        async def _execute(q, *a, **k):
            if "ADD COLUMN last_balance_check" in q:
                raise sqlite3.OperationalError("disk I/O error")
            return await real(q, *a, **k)

        monkeypatch.setattr(db, "execute", _execute)
        with caplog.at_level(logging.WARNING, logger="database.polymarket"):
            await PolymarketDBHandler(db).ensure_tables()
        assert any("last_balance_check" in r.getMessage() and "disk I/O error" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]
    finally:
        await db.close()
