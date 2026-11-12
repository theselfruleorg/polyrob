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
            return await build_ledger("alice", days=7, db=db)
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
            return await build_ledger("alice", days=7, db=db)
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
        "user_id": "alice", "window_days": 7,
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


# --- 116 W6.1: the no-container read path ---------------------------------------

def _no_container(monkeypatch):
    from core.container import DependencyContainer
    monkeypatch.setattr(DependencyContainer, "_instance", None)
    monkeypatch.delenv("DB_PATH", raising=False)


def test_no_container_reads_the_bot_db_read_only_and_never_creates_it(tmp_path, monkeypatch):
    _no_container(monkeypatch)
    led = _run(build_ledger("alice", days=1, data_dir=str(tmp_path)))
    want = str(tmp_path / "database" / "bot.db")
    assert led["costs_available"] is False and led["inbound_available"] is False
    assert led["costs_reason"] == f"no bot.db at {want}"
    note = ledger_availability_note(led)
    assert f"no bot.db at {want}" in note and "no usage_records table" not in note
    assert not (tmp_path / "database").exists()  # a status read creates nothing


def test_no_container_reads_real_rows_and_names_a_missing_table(tmp_path, monkeypatch):
    _no_container(monkeypatch)
    db = tmp_path / "database" / "bot.db"
    db.parent.mkdir()
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE usage_records (user_id TEXT, api_cost_usd REAL, cost REAL, "
                "timestamp TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.execute("INSERT INTO usage_records (user_id, api_cost_usd, cost) VALUES ('alice', 1.5, 0.01)")
    con.commit()
    con.close()
    before = sorted(p.name for p in db.parent.iterdir())
    led = _run(build_ledger("alice", days=1, data_dir=str(tmp_path)))
    assert led["costs_available"] is True and led["llm_calls"] == 1
    assert led["runtime"]["spend_window_usd"] == 1.5
    # the inbound leg failed for its own reason, not a fixed text
    assert led["inbound_available"] is False
    assert "x402_payment_requests" in led["inbound_reason"]
    assert sorted(p.name for p in db.parent.iterdir()) == before  # no -wal, no -shm


def test_off_the_main_thread_the_shared_connection_is_not_used(tmp_path, monkeypatch):
    """The snapshot's bridge read runs on a worker thread: it takes the read-only
    adaptor over the container's path, never the loop-bound shared connection."""
    import threading
    from core.container import DependencyContainer
    import modules.credits.unified_ledger as ul

    class _Shared:
        db_path = str(tmp_path / "database" / "bot.db")

    class _Dbm:
        connection = _Shared()

    class _Container:
        def get_service(self, name):
            return _Dbm()

    monkeypatch.setattr(DependencyContainer, "_instance", _Container())
    (tmp_path / "database").mkdir()
    sqlite3.connect(_Shared.db_path).close()
    out = {}
    t = threading.Thread(target=lambda: out.update(r=_run(ul._resolve_db(None, None))))
    t.start()
    t.join()
    db, reason = out["r"]
    assert reason is None and isinstance(db, ul.ReadOnlyBotDb)
    assert db.path == _Shared.db_path
    on_main = _run(ul._resolve_db(None, None))
    assert isinstance(on_main[0], _Dbm)
