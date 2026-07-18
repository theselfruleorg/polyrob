"""G-26 reachability fix (Task 5c follow-up, commit c3d7ed49).

Task 5c built a real `request_id` column + partial unique index + INSERT OR
IGNORE + migration -- all correct -- but the dedup was UNREACHABLE:
`record_llm_usage` had no `request_id` parameter and `_generate_request_id()`
returned a fresh `uuid.uuid4().hex` on EVERY call, so two billings of the SAME
completion always got different ids and the unique index never collided.
tests/unit/modules/credits/test_usage_records_dedup.py already covers the
ROW-level dedup by calling `_write_to_database` directly with a fixed
request_id -- that was never reachable from the real call path.

This suite drives the SAME dedup through `record_llm_usage` itself (the
actual entry point every billing call site uses), proving the fix reopened
the previously-dead path.
"""
import logging

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables
from modules.credits.usage_tracker import LLMUsageTracker


async def _make_tracker(tmp_path):
    db = DatabaseConnection(tmp_path / "dedup_reach.db")
    await db.connect()
    await db.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY)")
    await db.execute("INSERT OR IGNORE INTO user_profiles (user_id) VALUES ('u1')")
    await AuthTables(db).create_tables()

    t = LLMUsageTracker.__new__(LLMUsageTracker)
    t.db = db
    t.balance = None
    t.telemetry = None
    t.logger = logging.getLogger("g26-reachability-test")
    t._x402_session_tokens = {}
    t._tier_cache = {"u1": ""}  # non-x402/admin, but balance=None -> no deduction attempted
    return db, t


@pytest.mark.asyncio
async def test_record_llm_usage_same_request_id_twice_writes_one_row_and_warns(tmp_path, caplog):
    """The reachable case: a caller (e.g. a retried billing call site) invokes
    record_llm_usage TWICE with the SAME stable request_id -- simulating a
    retried bill of ONE completion. Must write exactly one row and log the
    duplicate-ignored WARN, driven through record_llm_usage (not
    _write_to_database directly)."""
    db, t = await _make_tracker(tmp_path)
    try:
        # "gpt-4o" is a real registry entry with pricing -- avoids an unrelated
        # "No pricing info for <model>" WARNING (a different logger) polluting
        # the count below.
        kw = dict(
            user_id="u1", session_id="s1", agent_id="a1", model="gpt-4o",
            provider="openai", input_tokens=100, output_tokens=50,
        )
        with caplog.at_level(logging.WARNING, logger="g26-reachability-test"):
            await t.record_llm_usage(request_id="resp:openai:chatcmpl_dup_1", **kw)
            await t.record_llm_usage(request_id="resp:openai:chatcmpl_dup_1", **kw)

        rows = await db.fetch_all(
            "SELECT * FROM usage_records WHERE request_id = 'resp:openai:chatcmpl_dup_1'"
        )
        assert len(rows) == 1, "a retried bill with the SAME request_id must not create a second row"

        # Scope to OUR tracker's logger -- caplog is process-wide and would
        # otherwise also catch unrelated warnings from other loggers.
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "g26-reachability-test"
        ]
        assert len(warnings) == 1, f"expected exactly one duplicate WARN, got {len(warnings)}"
        assert "resp:openai:chatcmpl_dup_1" in warnings[0].message
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_llm_usage_request_id_none_twice_writes_two_rows_no_false_dedup(tmp_path):
    """Legacy behavior preserved: when the caller has no stable id
    (request_id=None), each call gets a fresh uuid -- two genuinely separate
    calls must both persist, never falsely deduped."""
    db, t = await _make_tracker(tmp_path)
    try:
        kw = dict(
            user_id="u1", session_id="s1", agent_id="a1", model="claude-x",
            provider="anthropic", input_tokens=100, output_tokens=50,
        )
        rec1 = await t.record_llm_usage(request_id=None, **kw)
        rec2 = await t.record_llm_usage(request_id=None, **kw)

        assert rec1.request_id != rec2.request_id, "legacy fallback must generate distinct ids"

        rows = await db.fetch_all(
            "SELECT request_id FROM usage_records WHERE session_id = 's1'"
        )
        assert len(rows) == 2, "two calls with no stable id must both persist (no false dedup)"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_llm_usage_omitted_request_id_defaults_to_legacy_uuid_behavior(tmp_path):
    """Back-compat: callers that don't pass request_id at all (the parameter
    is optional) keep getting a fresh uuid per call, byte-identical to before
    this fix."""
    db, t = await _make_tracker(tmp_path)
    try:
        kw = dict(
            user_id="u1", session_id="s2", agent_id="a1", model="claude-x",
            provider="anthropic", input_tokens=10, output_tokens=5,
        )
        rec1 = await t.record_llm_usage(**kw)
        rec2 = await t.record_llm_usage(**kw)
        assert rec1.request_id != rec2.request_id

        rows = await db.fetch_all("SELECT request_id FROM usage_records WHERE session_id = 's2'")
        assert len(rows) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_llm_usage_distinct_request_ids_both_persist(tmp_path):
    """Sanity: two DIFFERENT stable ids (two genuinely different completions)
    both persist -- the dedup must not over-fire."""
    db, t = await _make_tracker(tmp_path)
    try:
        kw = dict(
            user_id="u1", session_id="s3", agent_id="a1", model="claude-x",
            provider="anthropic", input_tokens=10, output_tokens=5,
        )
        await t.record_llm_usage(request_id="resp:anthropic:msg_a", **kw)
        await t.record_llm_usage(request_id="resp:anthropic:msg_b", **kw)

        rows = await db.fetch_all(
            "SELECT request_id FROM usage_records WHERE session_id = 's3'"
        )
        assert {r["request_id"] for r in rows} == {"resp:anthropic:msg_a", "resp:anthropic:msg_b"}
    finally:
        await db.close()
