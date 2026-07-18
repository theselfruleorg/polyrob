"""G-26 (Task 5c): request_id becomes a real column with a partial unique
index, so a retry that re-enters `_write_to_database` with the SAME
request_id writes ONE row (not two) and surfaces exactly one WARN log
naming the request_id -- instead of silently double-writing (the old
behavior: request_id only lived inside the `metadata` JSON blob, invisible
to any DB constraint).

HONESTY NOTE (matches the comment in usage_tracker.py): this only tests the
ROW-level dedup in `usage_records`. Credit DEDUCTION dedup is a separate,
already-existing in-process mechanism (`_polyrob_billed` on the response
object) that this task does not touch.
"""
import logging
import types

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables
from modules.credits.usage_tracker import LLMUsageTracker


def _record(request_id="req-1", user_id="u1", session_id="s1"):
    return types.SimpleNamespace(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        agent_id="a1",
        model="gpt-4o",
        provider="openai",
        tokens=types.SimpleNamespace(prompt_tokens=100, completion_tokens=50, cached_tokens=0),
        costs=types.SimpleNamespace(
            api_cost_usd=0.01, credits_charged=2, markup_multiplier=1.0,
            credits_raw=2.0, user_cost_usd=0.02,
        ),
        duration_seconds=1.0, component="agent", purpose="next_action",
        success=True, error=None, metadata=None,
    )


async def _make_tracker(tmp_path):
    db = DatabaseConnection(tmp_path / "dedup.db")
    await db.connect()
    # usage_records.user_id has a FK -> user_profiles(user_id); a minimal
    # stand-in table is enough to satisfy PRAGMA foreign_keys=ON (only
    # existence + the referenced value matter, not the full user_profiles shape).
    await db.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY)")
    for uid in ("u1", "legacy"):
        await db.execute(
            "INSERT OR IGNORE INTO user_profiles (user_id) VALUES (?)", (uid,)
        )
    await AuthTables(db).create_tables()
    t = LLMUsageTracker.__new__(LLMUsageTracker)
    t.db = db
    t.logger = logging.getLogger("g26-dedup-test")
    return db, t


@pytest.mark.asyncio
async def test_same_request_id_twice_writes_one_row(tmp_path):
    db, t = await _make_tracker(tmp_path)
    try:
        rec = _record(request_id="dup-req-1")
        await t._write_to_database(rec)
        await t._write_to_database(rec)  # retry with the SAME request_id

        rows = await db.fetch_all(
            "SELECT * FROM usage_records WHERE request_id = 'dup-req-1'"
        )
        assert len(rows) == 1, "a duplicate request_id must not create a second row"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_duplicate_write_logs_exactly_one_warn_naming_request_id(tmp_path, caplog):
    db, t = await _make_tracker(tmp_path)
    try:
        rec = _record(request_id="dup-req-2")
        await t._write_to_database(rec)
        with caplog.at_level(logging.WARNING, logger="g26-dedup-test"):
            await t._write_to_database(rec)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"expected exactly one WARN, got {len(warnings)}"
        assert "dup-req-2" in warnings[0].message
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_first_write_of_a_request_id_logs_no_warning(tmp_path, caplog):
    db, t = await _make_tracker(tmp_path)
    try:
        rec = _record(request_id="fresh-req")
        with caplog.at_level(logging.WARNING, logger="g26-dedup-test"):
            await t._write_to_database(rec)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_distinct_request_ids_both_persist(tmp_path):
    db, t = await _make_tracker(tmp_path)
    try:
        await t._write_to_database(_record(request_id="req-a"))
        await t._write_to_database(_record(request_id="req-b"))

        rows = await db.fetch_all(
            "SELECT request_id FROM usage_records WHERE request_id IN ('req-a', 'req-b')"
        )
        assert {r["request_id"] for r in rows} == {"req-a", "req-b"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_request_id_column_and_metadata_both_populated(tmp_path):
    """Back-compat: request_id stays in the metadata JSON too, for any reader
    that hasn't been updated to read the new column."""
    import json

    db, t = await _make_tracker(tmp_path)
    try:
        await t._write_to_database(_record(request_id="both-places"))
        row = await db.fetch_one(
            "SELECT request_id, metadata FROM usage_records WHERE request_id = 'both-places'"
        )
        assert row["request_id"] == "both-places"
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        assert meta["request_id"] == "both-places"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_null_request_id_rows_are_unaffected_by_dedup(tmp_path):
    """Legacy rows (pre-migration, NULL request_id) must never collide with
    each other or with a real request_id -- the partial index exempts NULL."""
    db, t = await _make_tracker(tmp_path)
    try:
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost) "
            "VALUES ('legacy', 's1', 'llm_call', 1)"
        )
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost) "
            "VALUES ('legacy', 's1', 'llm_call', 1)"
        )
        await t._write_to_database(_record(request_id="new-req", user_id="legacy"))

        rows = await db.fetch_all(
            "SELECT request_id FROM usage_records WHERE user_id = 'legacy'"
        )
        assert len(rows) == 3
        null_rows = [r for r in rows if r["request_id"] is None]
        assert len(null_rows) == 2
    finally:
        await db.close()
