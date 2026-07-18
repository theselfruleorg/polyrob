"""Task 11 review fix C2: migrations/versions/v1_6_0_x402_tx_hash_unique.py --
the formal, version-tracked twin of the self-healing index creation in
`modules.database.x402_tables.X402Tables.create_tables()`.
"""
import sqlite3

import pytest

from migrations.versions.v1_6_0_x402_tx_hash_unique import (
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
    by tests/unit/migrations/test_baseline_records_to_canonical_table.py)."""

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


async def _index_names(db):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='x402_payment_requests'")
    return {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_upgrade_creates_unique_partial_index(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)

    ok = await upgrade(db, db_manager=None)
    assert ok is True
    assert "idx_x402_requests_tx_hash_unique" in await _index_names(db)
    assert await verify(db, db_manager=None) is True


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)

    await upgrade(db, db_manager=None)
    await upgrade(db, db_manager=None)  # must not raise
    assert "idx_x402_requests_tx_hash_unique" in await _index_names(db)


@pytest.mark.asyncio
async def test_index_rejects_real_duplicate_tx_hash_but_allows_many_nulls(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)
    await upgrade(db, db_manager=None)

    def _insert(id_, tx_hash):
        return db.execute(
            "INSERT INTO x402_payment_requests "
            "(id, amount, amount_usd, asset, chain, recipient, nonce, deadline, "
            " status, transaction_hash) "
            "VALUES (?, '1', 1.0, 'usdc', 'base', '0xT', ?, 0, 'pending', ?)",
            (id_, f"nonce-{id_}", tx_hash),
        )

    # many NULL transaction_hash rows must coexist (pending/expired invoices)
    await _insert("inv_a", None)
    await _insert("inv_b", None)
    # a real transaction_hash may be set once...
    await _insert("inv_c", "0xdup")
    # ...but never reused by a second row
    with pytest.raises(sqlite3.IntegrityError):
        await _insert("inv_d", "0xdup")


@pytest.mark.asyncio
async def test_upgrade_tolerant_of_missing_table(tmp_path):
    """A DB that doesn't have x402_payment_requests yet (e.g. a minimal legacy
    fixture, or this migration running before X402Tables.create_tables() has
    self-healed the table) must not crash boot -- PRAGMA table_info on a
    nonexistent table returns empty, not an error, so this skips cleanly."""
    db = _FakeDB(tmp_path / "no_table.db")
    await db.execute(_SCHEMA_VERSIONS)

    ok = await upgrade(db, db_manager=None)
    assert ok is True  # never raises


@pytest.mark.asyncio
async def test_downgrade_drops_index_and_version_record(tmp_path):
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)
    await upgrade(db, db_manager=None)
    assert "idx_x402_requests_tx_hash_unique" in await _index_names(db)

    ok = await downgrade(db, db_manager=None)
    assert ok is True
    assert "idx_x402_requests_tx_hash_unique" not in await _index_names(db)
    row = await db.fetch_one(
        "SELECT version FROM schema_versions WHERE version = ?", (VERSION,))
    assert row is None


def test_version_and_description_are_sane():
    assert VERSION == "1.6.0"
    assert "transaction_hash" in DESCRIPTION


# --- boot-crash-hazard follow-up: dedup legacy duplicate tx_hash rows ------
# (Task 11 Medium follow-up) A legacy DB that predates this index may already
# hold duplicate non-NULL transaction_hash rows -- exactly the historical C2
# bug the index guards against. CREATE UNIQUE INDEX over that data raises
# sqlite3.IntegrityError, which -- unguarded -- would crash `python -m
# migrations.migrate upgrade` outright. upgrade() must dedupe first.

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


@pytest.mark.asyncio
async def test_upgrade_dedupes_legacy_duplicate_tx_hash_before_creating_index(
        tmp_path, caplog):
    """Two rows sharing a non-NULL transaction_hash (a legacy DB with the
    historical C2 bug already applied) must not crash upgrade(); the earliest
    (by created_at) is kept, the later loses its tx_hash stamp (set to NULL,
    NOT deleted), and the affected request_ids are named in a loud log."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)

    await _insert_row(db, "inv_early", "0xdup", created_at="2026-01-01 00:00:00")
    await _insert_row(db, "inv_late", "0xdup", created_at="2026-06-01 00:00:00")

    with caplog.at_level(
            "WARNING", logger="migrations.versions.v1_6_0_x402_tx_hash_unique"):
        ok = await upgrade(db, db_manager=None)
    assert ok is True  # must not raise

    rows = await db.fetch_all(
        "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
    assert len(rows) == 2  # neither row deleted -- financial records
    by_id = {r["id"]: r["transaction_hash"] for r in rows}
    assert by_id["inv_early"] == "0xdup"  # earliest kept
    assert by_id["inv_late"] is None      # later cleared

    assert "idx_x402_requests_tx_hash_unique" in await _index_names(db)
    assert any("inv_early" in r.message and "inv_late" in r.message
               for r in caplog.records), (
        "expected a loud log naming both the kept and cleared request_ids")


@pytest.mark.asyncio
async def test_upgrade_dedup_rerun_is_idempotent_and_does_not_crash(tmp_path):
    """Re-running upgrade() after a dedup has already happened must be a
    no-op (the tx_hash is now unique) and must never raise."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)

    await _insert_row(db, "inv_early", "0xdup", created_at="2026-01-01 00:00:00")
    await _insert_row(db, "inv_late", "0xdup", created_at="2026-06-01 00:00:00")

    await upgrade(db, db_manager=None)
    ok = await upgrade(db, db_manager=None)  # second run must not raise
    assert ok is True

    rows = await db.fetch_all(
        "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
    by_id = {r["id"]: r["transaction_hash"] for r in rows}
    assert by_id == {"inv_early": "0xdup", "inv_late": None}
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_upgrade_fresh_db_no_duplicates_touches_no_rows(tmp_path):
    """A DB with no duplicate transaction_hash rows (including a totally
    fresh/empty one) must have the index created with every row untouched."""
    db = _FakeDB(tmp_path / "x402.db")
    await db.execute(_X402_PAYMENT_REQUESTS)
    await db.execute(_SCHEMA_VERSIONS)

    await _insert_row(db, "inv_a", "0xaaa")
    await _insert_row(db, "inv_b", "0xbbb")
    await _insert_row(db, "inv_c", None)

    ok = await upgrade(db, db_manager=None)
    assert ok is True

    rows = await db.fetch_all(
        "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
    by_id = {r["id"]: r["transaction_hash"] for r in rows}
    assert by_id == {"inv_a": "0xaaa", "inv_b": "0xbbb", "inv_c": None}
    assert "idx_x402_requests_tx_hash_unique" in await _index_names(db)
