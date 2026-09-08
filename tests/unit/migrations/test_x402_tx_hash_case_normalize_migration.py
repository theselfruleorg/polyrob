"""M1 (security audit 2026-08-22): migrations/versions/v1_8_0_x402_tx_hash_case_normalize.py --
the formal, version-tracked twin of the self-healing backfill in
`modules.database.x402_tables.X402Tables.create_tables()`
(`normalize_tx_hash_case`).

Mirrors the structure of test_x402_tx_hash_unique_migration.py (same _FakeDB
double, same table DDL) so this stays consistent with the existing migration
test pattern rather than introducing a new harness.
"""
import sqlite3

import pytest

from migrations.versions.v1_8_0_x402_tx_hash_case_normalize import (
    DESCRIPTION, VERSION, downgrade, upgrade, verify,
)

_X402_PAYMENT_REQUESTS = """
CREATE TABLE x402_payment_requests (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    payer_address TEXT,
    amount TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    asset TEXT NOT NULL,
    chain TEXT NOT NULL,
    recipient TEXT NOT NULL,
    nonce TEXT UNIQUE NOT NULL,
    deadline INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    transaction_hash TEXT,
    payment_id TEXT,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# The partial unique index from v1.6.0 -- present on every real DB this
# migration will ever run against. Created here explicitly so the test
# exercises the SAME money-critical constraint the real migration order
# (dedupe_and_create_tx_hash_unique_index BEFORE normalize_tx_hash_case)
# relies on for safety.
_TX_HASH_UNIQUE_INDEX = """
CREATE UNIQUE INDEX idx_x402_requests_tx_hash_unique
ON x402_payment_requests(transaction_hash)
WHERE transaction_hash IS NOT NULL
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


class _FakeDB:
    """A minimal sync-sqlite-backed async db double (mirrors the pattern used
    by tests/unit/migrations/test_x402_tx_hash_unique_migration.py)."""

    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row

    async def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    async def fetch_one(self, sql, params=()):
        return self._conn.execute(sql, params).fetchone()

    async def fetch_all(self, sql, params=()):
        return self._conn.execute(sql, params).fetchall()


def _insert_row(db, id_, tx_hash, created_at=None, status="completed"):
    if created_at is None:
        return db.execute(
            "INSERT INTO x402_payment_requests "
            "(id, amount, amount_usd, asset, chain, recipient, nonce, deadline, "
            " status, transaction_hash) "
            "VALUES (?, '1', 1.0, 'usdc', 'base', '0xT', ?, 0, ?, ?)",
            (id_, f"nonce-{id_}", status, tx_hash),
        )
    return db.execute(
        "INSERT INTO x402_payment_requests "
        "(id, amount, amount_usd, asset, chain, recipient, nonce, deadline, "
        " status, transaction_hash, created_at) "
        "VALUES (?, '1', 1.0, 'usdc', 'base', '0xT', ?, 0, ?, ?, ?)",
        (id_, f"nonce-{id_}", status, tx_hash, created_at),
    )


async def _all_hashes(db):
    rows = await db.fetch_all("SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
    return {r["id"]: r["transaction_hash"] for r in rows}


def test_version_and_description_are_sane():
    assert VERSION == "1.8.0"
    assert "transaction_hash" in DESCRIPTION


@pytest.mark.asyncio
async def test_upgrade_tolerant_of_missing_table(tmp_path):
    """PRAGMA table_info on a nonexistent table returns empty, not an error --
    must not crash boot/migration when x402_payment_requests doesn't exist yet."""
    db = _FakeDB(tmp_path / "no_table.db")
    await db.execute(_SCHEMA_VERSIONS)

    ok = await upgrade(db, db_manager=None)
    assert ok is True


@pytest.mark.asyncio
async def test_upgrade_lowercases_mixed_case_rows(tmp_path):
    """The core backfill: a legacy row settled with a mixed-case (e.g.
    checksummed-looking) hash must come out lowercase so the now-normalized
    replay guard (modules.x402.invoicing._norm_tx) can find it."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)

    await _insert_row(db, "inv_a", "0xDEADBEEFCafeBabe0000000000000000000000000000000000000001")
    await _insert_row(db, "inv_b", None)  # pending invoice, never settled -- must stay NULL

    ok = await upgrade(db, db_manager=None)
    assert ok is True

    hashes = await _all_hashes(db)
    assert hashes["inv_a"] == "0xdeadbeefcafebabe0000000000000000000000000000000000000001"
    assert hashes["inv_b"] is None

    row = await db.fetch_one("SELECT version FROM schema_versions WHERE version = ?", (VERSION,))
    assert row is not None
    assert await verify(db, db_manager=None) is True


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)
    await _insert_row(db, "inv_a", "0xABCDEF")

    await upgrade(db, db_manager=None)
    ok = await upgrade(db, db_manager=None)  # second run must not raise
    assert ok is True

    hashes = await _all_hashes(db)
    assert hashes["inv_a"] == "0xabcdef"


@pytest.mark.asyncio
async def test_upgrade_fresh_db_no_rows_touches_nothing(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)

    ok = await upgrade(db, db_manager=None)
    assert ok is True
    assert await _all_hashes(db) == {}


@pytest.mark.asyncio
async def test_upgrade_already_lowercase_rows_untouched(tmp_path):
    """A DB where every hash is already lowercase (the common post-fix case)
    is a no-op: same values, same rows."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)
    await _insert_row(db, "inv_a", "0xaaaa")
    await _insert_row(db, "inv_b", "0xbbbb")

    ok = await upgrade(db, db_manager=None)
    assert ok is True
    assert await _all_hashes(db) == {"inv_a": "0xaaaa", "inv_b": "0xbbbb"}


# --- ⚠️ money-critical: the case-collision guard --------------------------

@pytest.mark.asyncio
async def test_upgrade_leaves_case_collision_rows_untouched_and_logs_loudly(tmp_path, caplog):
    """Two rows whose transaction_hash differ ONLY by case (0xDUPE vs 0xdupe)
    are the M1 double-settle SIGNATURE -- a real payment settled two
    invoices because the old case-sensitive guard missed the reuse. The
    backfill must NOT silently merge/lower them (that would be guessing
    which row is the "real" one) -- it must leave BOTH exactly as stored and
    log the collision loudly for owner reconciliation, and it must NOT raise
    (the money-critical requirement: never fail the migration)."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)

    await _insert_row(db, "inv_early", "0xDUPE", created_at="2026-01-01 00:00:00")
    await _insert_row(db, "inv_late", "0xdupe", created_at="2026-06-01 00:00:00")
    # a normal, non-colliding row must still get lowercased
    await _insert_row(db, "inv_safe", "0xSAFE")

    with caplog.at_level(
            "WARNING", logger="modules.database.x402_tables"):
        ok = await upgrade(db, db_manager=None)
    assert ok is True  # must never raise, even with a collision present

    hashes = await _all_hashes(db)
    # both colliding rows preserved EXACTLY as stored -- no merge, no guess
    assert hashes["inv_early"] == "0xDUPE"
    assert hashes["inv_late"] == "0xdupe"
    # the unrelated safe row is still backfilled
    assert hashes["inv_safe"] == "0xsafe"

    assert any(
        "inv_early" in r.message and "inv_late" in r.message and "COLLISION" in r.message
        for r in caplog.records
    ), "expected a loud WARNING naming both colliding request_ids"


@pytest.mark.asyncio
async def test_upgrade_collision_group_of_three_all_preserved(tmp_path):
    """A collision group need not be exactly 2 rows -- three differently-cased
    variants of the same hash must ALL be left untouched."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)

    await _insert_row(db, "inv_1", "0xTriple", created_at="2026-01-01 00:00:00")
    await _insert_row(db, "inv_2", "0xtriple", created_at="2026-02-01 00:00:00")
    await _insert_row(db, "inv_3", "0xTRIPLE", created_at="2026-03-01 00:00:00")

    ok = await upgrade(db, db_manager=None)
    assert ok is True

    hashes = await _all_hashes(db)
    assert hashes == {"inv_1": "0xTriple", "inv_2": "0xtriple", "inv_3": "0xTRIPLE"}


@pytest.mark.asyncio
async def test_upgrade_collision_rerun_is_idempotent(tmp_path):
    """Re-running upgrade() after a collision was already detected must not
    raise and must not change anything further (nothing new to detect)."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)
    await _insert_row(db, "inv_a", "0xDUPE")
    await _insert_row(db, "inv_b", "0xdupe")

    await upgrade(db, db_manager=None)
    ok = await upgrade(db, db_manager=None)
    assert ok is True
    assert await _all_hashes(db) == {"inv_a": "0xDUPE", "inv_b": "0xdupe"}


@pytest.mark.asyncio
async def test_downgrade_removes_version_record_only(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_TX_HASH_UNIQUE_INDEX)
    await db.execute(_SCHEMA_VERSIONS)
    await _insert_row(db, "inv_a", "0xABCDEF")
    await upgrade(db, db_manager=None)

    ok = await downgrade(db, db_manager=None)
    assert ok is True
    row = await db.fetch_one("SELECT version FROM schema_versions WHERE version = ?", (VERSION,))
    assert row is None
    # data backfill is not destructive/reversible -- the lowercase value stays
    hashes = await _all_hashes(db)
    assert hashes["inv_a"] == "0xabcdef"
