"""F17: `usage_records.cache_creation_tokens` must become a real column on BOTH
a fresh DB and a legacy DB that predates it -- and must never crash boot.

Modelled on tests/unit/modules/database/test_usage_records_request_id_migration.py
(the G-26 retrofit), because the same two independent paths must both be safe
against a legacy `usage_records` table:
  1. `AuthTables.create_tables()` -- the inline creator, which self-heals the
     column on every boot (a fresh install STAMPS migrations without executing
     them, so the inline schema is the only thing that actually runs there).
  2. `migrations/versions/v1_10_0_usage_records_cache_creation.py` -- the
     canonical runner, exercised directly and through `apply_migrations_at_boot`.

Why the column matters: a cache WRITE costs MORE than an uncached token
(1.25x on Anthropic's 5-minute window, 2x on the 1-hour one). `calculate_cost`
has billed it separately since G3, but the row stored only cache READS -- so
after the fact nothing could say whether a session's cache paid for itself.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables

# The pre-F17 usage_records shape -- what an existing production bot.db looks
# like: request_id is present (G-26 shipped), cache_creation_tokens is not.
_LEGACY_USAGE_RECORDS = """
CREATE TABLE usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    cost INTEGER NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    api_cost_usd REAL DEFAULT 0.0,
    markup_multiplier REAL DEFAULT 1.0,
    request_id TEXT,
    metadata TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_SCHEMA_VERSIONS = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT DEFAULT 'system',
    checksum TEXT,
    execution_time_ms INTEGER
)
"""


async def _columns(db, table):
    return {r["name"] for r in await db.fetch_all(f"PRAGMA table_info({table})")}


# ── AuthTables.create_tables() self-heal ─────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_tables_backfills_cache_creation_onto_legacy_table(tmp_path):
    db = DatabaseConnection(tmp_path / "legacy.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost) "
            "VALUES ('u1', 's1', 'llm_call', 1)"
        )

        await AuthTables(db).create_tables()   # must NOT raise

        assert "cache_creation_tokens" in await _columns(db, "usage_records")

        # The pre-existing row is backfilled with the column DEFAULT, not
        # dropped — and 0 is the honest value for a row written before the
        # number was recorded at all.
        row = await db.fetch_one(
            "SELECT cache_creation_tokens FROM usage_records WHERE user_id='u1'"
        )
        assert row is not None
        assert (row["cache_creation_tokens"] or 0) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auth_tables_idempotent_on_fresh_db(tmp_path):
    db = DatabaseConnection(tmp_path / "fresh.db")
    await db.connect()
    try:
        await AuthTables(db).create_tables()
        await AuthTables(db).create_tables()   # no duplicate-column error
        assert "cache_creation_tokens" in await _columns(db, "usage_records")
    finally:
        await db.close()


# ── the formal migrations/versions/v1_10_0 file ──────────────────────────────

@pytest.mark.asyncio
async def test_migration_upgrade_applies_to_legacy_db(tmp_path):
    from migrations.versions.v1_10_0_usage_records_cache_creation import upgrade, verify

    db = DatabaseConnection(tmp_path / "legacy_migrate.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute(_SCHEMA_VERSIONS)

        await upgrade(db, db_manager=None)

        assert "cache_creation_tokens" in await _columns(db, "usage_records")
        assert await verify(db, db_manager=None) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_upgrade_tolerant_of_already_applied(tmp_path):
    """The inline creator may have self-healed the column earlier in the same
    boot, so a second upgrade() must be a no-op rather than an error."""
    from migrations.versions.v1_10_0_usage_records_cache_creation import upgrade

    db = DatabaseConnection(tmp_path / "twice.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute(_SCHEMA_VERSIONS)

        await upgrade(db, db_manager=None)
        await upgrade(db, db_manager=None)   # must not raise

        assert "cache_creation_tokens" in await _columns(db, "usage_records")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_preserves_existing_rows(tmp_path):
    """An additive, defaulted column must not disturb a single billed row."""
    from migrations.versions.v1_10_0_usage_records_cache_creation import upgrade

    db = DatabaseConnection(tmp_path / "rows.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute(_SCHEMA_VERSIONS)
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost, "
            "input_tokens, output_tokens, cached_tokens, api_cost_usd) "
            "VALUES ('u1', 's1', 'llm_call', 7, 100, 20, 80, 0.5)"
        )

        await upgrade(db, db_manager=None)

        row = await db.fetch_one("SELECT * FROM usage_records WHERE user_id='u1'")
        assert row["cost"] == 7
        assert row["input_tokens"] == 100 and row["cached_tokens"] == 80
        assert (row["cache_creation_tokens"] or 0) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_migrations_at_boot_retrofits_legacy_db(tmp_path):
    """End-to-end through the canonical runner against the REAL versions dir."""
    from migrations.boot import apply_migrations_at_boot
    from migrations.version_manager import DatabaseVersionManager

    db = DatabaseConnection(tmp_path / "boot_legacy.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)

        vm = DatabaseVersionManager(db)
        await vm.initialize()
        for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0",
                  "1.5.0", "1.6.0", "1.7.0", "1.8.0", "1.9.0"):
            await vm.record_migration(v, f"prestamp {v}")

        summary = await apply_migrations_at_boot(db)

        assert summary["error"] is None
        assert "1.10.0" in summary["applied"]
        assert "cache_creation_tokens" in await _columns(db, "usage_records")
    finally:
        await db.close()
