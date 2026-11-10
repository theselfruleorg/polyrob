"""EP-3: ``fetch_all`` used to swallow every error and return ``[]``, so a
locked or broken store read as "no rows" (a reaper skipped its work, a PRAGMA
probe answered "table missing"). It now raises like ``fetch_one``."""
import logging

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.x402_tables import _payment_request_cols, add_invoice_state_columns


@pytest.mark.asyncio
async def test_fetch_all_raises_when_the_read_fails(tmp_path):
    db = DatabaseConnection(tmp_path / "x.db")
    await db.connect()
    try:
        with pytest.raises(Exception):
            await db.fetch_all("SELECT * FROM no_such_table")
        assert await db.fetch_all("PRAGMA table_info(no_such_table)") == []  # absent = empty
    finally:
        await db.close()


class _Unreadable:
    async def fetch_all(self, *_a, **_k):
        raise RuntimeError("database is locked")

    async def execute(self, *_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("no DDL on an unreadable store")


@pytest.mark.asyncio
async def test_unreadable_payment_table_is_not_reported_absent(caplog):
    log = logging.getLogger("test.x402_tables")
    with caplog.at_level(logging.INFO, logger="test.x402_tables"):
        assert await _payment_request_cols(_Unreadable(), log, "the step") is None
        await add_invoice_state_columns(_Unreadable(), log)
    text = caplog.text
    assert "could not read x402_payment_requests" in text
    assert "not present" not in text
