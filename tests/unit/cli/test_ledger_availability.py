"""H14b (2026-07-15): the unified ledger must distinguish "no data yet /
metering degraded" from a genuine $0.00.

Each ledger leg now carries an availability marker so a MISSING/corrupt money
table (usage_records, x402_payment_requests) is not silently rendered as an
honest-looking zero. The finance/journey views key off these markers.
"""
import asyncio
import sqlite3

from modules.credits.unified_ledger import (build_ledger, format_ledger,
                                             ledger_availability_note)
from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables


def _run(coro):
    return asyncio.run(coro)


def test_missing_tables_mark_legs_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    db_path = tmp_path / "bot.db"
    sqlite3.connect(str(db_path)).close()  # empty: no usage_records/x402 tables

    async def run():
        db = DatabaseConnection(db_path)
        await db.connect()
        try:
            return await build_ledger("rob", days=7, db=db)
        finally:
            await db.close()

    led = _run(run())
    assert led["costs_available"] is False
    assert led["inbound_available"] is False
    note = ledger_availability_note(led)
    assert note is not None
    assert "no data yet" in note.lower()
    # format_ledger must carry the honesty note, not a bare $0.00 sheet.
    assert "no data yet" in format_ledger(led).lower()


def test_present_tables_mark_inbound_available(tmp_path):
    db_path = tmp_path / "bot.db"

    async def setup():
        db = DatabaseConnection(db_path)
        await db.connect()
        await UserProfiles(db).create_table()
        await X402Tables(db).create_tables()
        return db

    async def run():
        db = await setup()
        try:
            return await build_ledger("rob", days=7, db=db)
        finally:
            await db.close()

    led = _run(run())
    assert led["inbound_available"] is True
    # inbound present => not the "no data yet" empty-state (costs may still be
    # unavailable if usage_records was not created — that's a partial degrade).
    note = ledger_availability_note(led)
    assert not (note and note.lower().startswith("no data yet"))


def test_note_flags_wallet_metering_off():
    led = {
        "user_id": "rob", "window_days": 7,
        "llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0,
        "costs_available": True,
        "wallet_spend_usd": 0.0, "wallet_payments": 0, "wallet_metering": "disabled",
        "settled_payments": 0,
        "pending_invoices_usd": 0.0, "pending_invoices": 0, "inbound_available": True,
    }
    note = ledger_availability_note(led)
    assert note is not None
    assert "metering degraded" in note.lower()
    assert "wallet-spend metering off" in note.lower()
