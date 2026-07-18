"""G-42 — x402_access_log was dead DDL: X402Tables.create_tables() (and the
mirrored modules/database/schema.sql block) created the table + two indices,
but no code anywhere ever INSERTed into it or read from it (grepped: zero
readers, zero writers, no test references the literal table name). Removed
rather than wired, per the "simplest correct action is REMOVE the dead DDL"
guidance -- there was no concrete audit-consumer to wire an honest write for.

This test locks in the removal: a fresh DB created via X402Tables.create_tables()
must NOT contain the table, and the rest of X402Tables' tables/indices must
still be created without error (schema surface reduction, not a functional
regression).
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.x402_tables import X402Tables


@pytest.mark.asyncio
async def test_create_tables_does_not_create_dead_access_log_table(tmp_path):
    db = DatabaseConnection(tmp_path / "fresh.db")
    await db.connect()
    try:
        await X402Tables(db).create_tables()  # must not raise

        table = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='x402_access_log'")
        assert table is None, "x402_access_log is dead DDL and should not be created"

        for index_name in ("idx_x402_access_payer", "idx_x402_access_endpoint"):
            idx = await db.fetch_one(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,))
            assert idx is None, f"{index_name} belonged to the removed table"

        # The real x402 tables are still created — this wasn't a wholesale break.
        for table_name in (
            "x402_payment_requests", "settlement_scan", "subscriptions",
            "subscription_applied_settlements",
        ):
            row = await db.fetch_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,))
            assert row is not None, f"{table_name} should still be created"
    finally:
        await db.close()
