"""G-26 (Task 5c): usage_records.request_id must become a real column (with a
partial unique index) on BOTH a fresh DB and a legacy DB that predates it --
without ever crashing boot the way the pre-existing user_profiles/tier bug
did (see test_user_profiles_migration.py -- CREATE INDEX against a column
that doesn't exist yet raises and, in AuthTables.create_tables(), that
exception propagates all the way up through DatabaseManager._init_tables()
and crashes app boot).

Covers two independent retrofit paths that both must be safe against a
legacy `usage_records` table with no `request_id` column:
  1. `AuthTables.create_tables()` (self-heals on every boot, same idiom as
     the existing auth_nonces/chain_id backfill in the same file).
  2. `migrations/versions/v1_5_0_usage_records_request_id.py` (the canonical
     migrations runner, exercised directly and via `apply_migrations_at_boot`
     against a legacy DB fixture that predates the column).
"""
import asyncio

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.auth_tables import AuthTables

# The pre-G-26 usage_records shape (no request_id column) -- what an
# existing production bot.db actually looks like today.
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
    metadata TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


async def _columns(db, table):
    return {r["name"] for r in await db.fetch_all(f"PRAGMA table_info({table})")}


async def _index_names(db):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='usage_records'"
    )
    return {r["name"] for r in rows}


async def _seed_user_profiles_stub(db, *user_ids):
    """usage_records.user_id has a FK -> user_profiles(user_id); a minimal
    stand-in table/row is enough to satisfy PRAGMA foreign_keys=ON."""
    await db.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY)")
    for uid in user_ids:
        await db.execute("INSERT OR IGNORE INTO user_profiles (user_id) VALUES (?)", (uid,))


# ── AuthTables.create_tables() self-heal ─────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_tables_backfills_request_id_onto_legacy_table(tmp_path):
    db = DatabaseConnection(tmp_path / "legacy.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost) "
            "VALUES ('u1', 's1', 'llm_call', 1)"
        )

        # Must NOT raise (this is exactly the user_profiles/tier bug shape:
        # CREATE [UNIQUE] INDEX against a column that doesn't exist yet).
        await AuthTables(db).create_tables()

        cols = await _columns(db, "usage_records")
        assert "request_id" in cols
        assert "idx_usage_records_request_id" in await _index_names(db)

        # Pre-existing row backfilled with NULL, not dropped/corrupted.
        row = await db.fetch_one("SELECT request_id FROM usage_records WHERE user_id='u1'")
        assert row is not None
        assert row["request_id"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auth_tables_idempotent_on_fresh_db(tmp_path):
    db = DatabaseConnection(tmp_path / "fresh.db")
    await db.connect()
    try:
        await AuthTables(db).create_tables()
        # Second call is a no-op (no duplicate-column / duplicate-index errors).
        await AuthTables(db).create_tables()
        assert "request_id" in await _columns(db, "usage_records")
        assert "idx_usage_records_request_id" in await _index_names(db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_unique_index_rejects_real_duplicate_but_allows_many_nulls(tmp_path):
    db = DatabaseConnection(tmp_path / "idx.db")
    await db.connect()
    try:
        await _seed_user_profiles_stub(db, "u1")
        await AuthTables(db).create_tables()

        # Many NULL request_id rows must coexist (legacy rows / no-request_id
        # callers) -- NULL is exempt from the partial unique index.
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost) "
            "VALUES ('u1', 's1', 'llm_call', 1)"
        )
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost) "
            "VALUES ('u1', 's1', 'llm_call', 1)"
        )

        # A real duplicate request_id must be rejected at the DB level.
        await db.execute(
            "INSERT INTO usage_records (user_id, session_id, resource_type, cost, request_id) "
            "VALUES ('u1', 's1', 'llm_call', 1, 'dup-1')"
        )
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO usage_records (user_id, session_id, resource_type, cost, request_id) "
                "VALUES ('u1', 's1', 'llm_call', 1, 'dup-1')"
            )
    finally:
        await db.close()


# ── formal migrations/versions/v1_5_0 file ───────────────────────────────────

@pytest.mark.asyncio
async def test_migration_upgrade_applies_to_legacy_db(tmp_path):
    from migrations.versions.v1_5_0_usage_records_request_id import upgrade, verify

    db = DatabaseConnection(tmp_path / "legacy_migrate.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_by TEXT DEFAULT 'system',
                checksum TEXT,
                execution_time_ms INTEGER
            )
        """)

        await upgrade(db, db_manager=None)

        assert "request_id" in await _columns(db, "usage_records")
        assert "idx_usage_records_request_id" in await _index_names(db)
        assert await verify(db, db_manager=None) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_upgrade_tolerant_of_already_applied(tmp_path):
    """Running upgrade() twice (e.g. AuthTables already self-healed the column
    before the formal migration runs in the same boot) must not raise."""
    from migrations.versions.v1_5_0_usage_records_request_id import upgrade

    db = DatabaseConnection(tmp_path / "twice.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schema_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_by TEXT DEFAULT 'system',
                checksum TEXT,
                execution_time_ms INTEGER
            )
        """)

        await upgrade(db, db_manager=None)
        await upgrade(db, db_manager=None)  # must not raise

        assert "request_id" in await _columns(db, "usage_records")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_migrations_at_boot_retrofits_legacy_db(tmp_path):
    """End-to-end through the canonical runner (migrations/boot.py) against the
    REAL migrations/versions/ directory -- a DB already stamped through 1.4.0
    (genuinely pending 1.5.0) gets the column + index without executing
    earlier already-applied migrations."""
    from migrations.boot import apply_migrations_at_boot
    from migrations.version_manager import DatabaseVersionManager

    db = DatabaseConnection(tmp_path / "boot_legacy.db")
    await db.connect()
    try:
        await db.execute(_LEGACY_USAGE_RECORDS)

        vm = DatabaseVersionManager(db)
        await vm.initialize()
        for v in ("1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"):
            await vm.record_migration(v, f"prestamp {v}")

        summary = await apply_migrations_at_boot(db)

        assert summary["error"] is None
        assert "1.5.0" in summary["applied"]
        assert "request_id" in await _columns(db, "usage_records")
        assert "idx_usage_records_request_id" in await _index_names(db)
    finally:
        await db.close()
