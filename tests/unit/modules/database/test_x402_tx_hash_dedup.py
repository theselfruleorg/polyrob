"""Boot-crash-hazard follow-up (Task 11 review, Medium): X402Tables.create_tables()
adds a partial UNIQUE index on x402_payment_requests.transaction_hash. On a legacy
DB that already contains duplicate non-NULL transaction_hash rows (exactly the
historical C2 bug the index guards against), CREATE UNIQUE INDEX raises
sqlite3.IntegrityError -- and with no per-table isolation in
DatabaseManager._init_tables(), that would crash app boot. create_tables() must
deduplicate (keep the earliest row, NULL the tx stamp on the rest -- never
delete rows) before creating the index, and must degrade (not crash) if the
index still can't be created.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.x402_tables import X402Tables

# The pre-Task-11 shape: no idx_x402_requests_tx_hash_unique index yet, so two
# rows sharing a non-NULL transaction_hash could exist (the historical C2 bug).
_LEGACY_X402_PAYMENT_REQUESTS = """
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


async def _legacy_db(tmp_path, name="legacy.db"):
    db = DatabaseConnection(tmp_path / name)
    await db.connect()
    await db.execute(_LEGACY_X402_PAYMENT_REQUESTS)
    return db


async def _insert(db, id_, tx_hash, created_at, status="completed"):
    await db.execute(
        "INSERT INTO x402_payment_requests "
        "(id, amount, amount_usd, asset, chain, recipient, nonce, deadline, "
        " status, transaction_hash, created_at) "
        "VALUES (?, '1', 1.0, 'usdc', 'base', '0xT', ?, 0, ?, ?, ?)",
        (id_, f"nonce-{id_}", status, tx_hash, created_at),
    )


@pytest.mark.asyncio
async def test_create_tables_dedupes_legacy_duplicate_tx_hash_then_succeeds(
        tmp_path, caplog):
    """A legacy DB with two rows sharing a non-NULL transaction_hash must not
    crash create_tables(); the earliest row (by created_at) keeps the hash,
    the later row's hash is cleared to NULL (row NOT deleted -- it's a
    financial record), and both request_ids are named in a loud log."""
    db = await _legacy_db(tmp_path)
    try:
        await _insert(db, "inv_early", "0xdup", "2026-01-01 00:00:00")
        await _insert(db, "inv_late", "0xdup", "2026-06-01 00:00:00")

        with caplog.at_level("WARNING", logger="database.x402_tables"):
            await X402Tables(db).create_tables()  # must NOT raise

        rows = await db.fetch_all(
            "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
        assert len(rows) == 2  # neither row deleted
        by_id = {r["id"]: r["transaction_hash"] for r in rows}
        assert by_id["inv_early"] == "0xdup"
        assert by_id["inv_late"] is None

        index = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_x402_requests_tx_hash_unique'")
        assert index is not None

        assert any("inv_early" in r.message and "inv_late" in r.message
                   for r in caplog.records), (
            "expected a loud log naming both the kept and cleared request_ids")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_tables_dedup_survives_three_way_duplicate(tmp_path):
    """Three rows sharing one transaction_hash: only the earliest keeps it,
    the other two are cleared -- and all three rows still exist."""
    db = await _legacy_db(tmp_path)
    try:
        await _insert(db, "inv_1", "0xtriple", "2026-01-01 00:00:00")
        await _insert(db, "inv_2", "0xtriple", "2026-02-01 00:00:00")
        await _insert(db, "inv_3", "0xtriple", "2026-03-01 00:00:00")

        await X402Tables(db).create_tables()  # must NOT raise

        rows = await db.fetch_all(
            "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
        assert len(rows) == 3
        by_id = {r["id"]: r["transaction_hash"] for r in rows}
        assert by_id["inv_1"] == "0xtriple"
        assert by_id["inv_2"] is None
        assert by_id["inv_3"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_tables_is_idempotent_after_dedup(tmp_path):
    """Calling create_tables() again after a dedup has already run must be a
    no-op (already unique) and must never raise."""
    db = await _legacy_db(tmp_path)
    try:
        await _insert(db, "inv_early", "0xdup", "2026-01-01 00:00:00")
        await _insert(db, "inv_late", "0xdup", "2026-06-01 00:00:00")

        await X402Tables(db).create_tables()
        await X402Tables(db).create_tables()  # second call must not raise

        rows = await db.fetch_all(
            "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
        by_id = {r["id"]: r["transaction_hash"] for r in rows}
        assert by_id == {"inv_early": "0xdup", "inv_late": None}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_tables_fresh_db_no_duplicates_touches_no_rows(tmp_path):
    """A fresh DB (no rows at all) must have the index created with nothing
    to deduplicate -- the common, zero-cost case."""
    db = DatabaseConnection(tmp_path / "fresh.db")
    await db.connect()
    try:
        await X402Tables(db).create_tables()

        index = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_x402_requests_tx_hash_unique'")
        assert index is not None

        rows = await db.fetch_all("SELECT * FROM x402_payment_requests")
        assert rows == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_tables_leaves_non_duplicate_rows_untouched(tmp_path):
    """Rows with unique (or NULL) transaction_hash values must not be
    modified by the dedup pass."""
    db = await _legacy_db(tmp_path)
    try:
        await _insert(db, "inv_a", "0xaaa", "2026-01-01 00:00:00")
        await _insert(db, "inv_b", "0xbbb", "2026-01-02 00:00:00")
        await _insert(db, "inv_c", None, "2026-01-03 00:00:00")

        await X402Tables(db).create_tables()

        rows = await db.fetch_all(
            "SELECT id, transaction_hash FROM x402_payment_requests ORDER BY id")
        by_id = {r["id"]: r["transaction_hash"] for r in rows}
        assert by_id == {"inv_a": "0xaaa", "inv_b": "0xbbb", "inv_c": None}
    finally:
        await db.close()
