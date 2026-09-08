"""M1 (security audit 2026-08-22): the settlement replay guard must compare
`transaction_hash` case-insensitively.

The on-chain scanner's hash comes from `eth_getLogs` (lowercase,
`modules/x402/onchain_probe.py`); a facilitator's settlement hash
(`modules/x402/middleware.py`, `api/x402_endpoints.py`) is stored whatever
case it returns. When `/pay` settles invoice X, that settlement is ITSELF a
USDC transfer into the treasury, so a later on-chain scan re-observes it. A
case-sensitive compare missed that replay and let
`match_pending_invoice_by_amount` settle a DIFFERENT same-amount pending
invoice from the SAME real payment -- a second invoice marked paid with no
money behind it.

Runs against the REAL x402 schema (X402Tables.create_tables), same pattern
as test_invoicing.py and test_settlement_watcher_onchain.py -- no new DB
harness.
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "0xTreasuryAddress000000000000000000000001"


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER"):
        monkeypatch.delenv(var, raising=False)


# --- Step 1 (brief): the exact bug scenario, both directions --------------

@pytest.mark.asyncio
async def test_replay_guard_is_case_insensitive_mixed_then_lower(tmp_path):
    """M1: the facilitator returns a checksummed/mixed-case hash for invoice
    A's settlement; the later on-chain scan (always lowercase) re-observes
    the SAME transfer and must be refused for invoice B, not allowed to
    settle a second, unrelated invoice at the same amount."""
    db = await _setup_db(tmp_path)
    try:
        inv_a = await invoicing.create_payment_request(
            user_id="rob", session_id="s_a", amount_usd=5.0, purpose="a", db=db)
        mixed = "0xDEADBEEFCafeBabe0000000000000000000000000000000000000001"
        assert await invoicing.settle_payment_request(
            inv_a["request_id"], transaction_hash=mixed, db=db) is True

        inv_b = await invoicing.create_payment_request(
            user_id="rob", session_id="s_b", amount_usd=5.0, purpose="b", db=db)
        lower = mixed.lower()
        assert await invoicing.settle_payment_request(
            inv_b["request_id"], transaction_hash=lower, db=db) is False

        row_b = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (inv_b["request_id"],))
        assert row_b["status"] == "pending"  # never settled by a consumed tx
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_replay_guard_is_case_insensitive_lower_then_mixed(tmp_path):
    """Same invariant, the other direction: settle first with the lowercase
    form (as the on-chain scanner would produce), then a mixed-case replay
    of the SAME tx (e.g. a facilitator echo, or an owner CLI paste) must
    also be refused."""
    db = await _setup_db(tmp_path)
    try:
        inv_a = await invoicing.create_payment_request(
            user_id="rob", session_id="s_a", amount_usd=5.0, purpose="a", db=db)
        lower = "0xdeadbeefcafebabe0000000000000000000000000000000000000002"
        assert await invoicing.settle_payment_request(
            inv_a["request_id"], transaction_hash=lower, db=db) is True

        inv_b = await invoicing.create_payment_request(
            user_id="rob", session_id="s_b", amount_usd=5.0, purpose="b", db=db)
        mixed = lower.upper().replace("0X", "0x")
        assert await invoicing.settle_payment_request(
            inv_b["request_id"], transaction_hash=mixed, db=db) is False

        row_b = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?", (inv_b["request_id"],))
        assert row_b["status"] == "pending"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settle_stores_the_normalized_lowercase_form(tmp_path):
    """The UPDATE itself must stamp the lowercase canonical form -- not the
    raw mixed-case input -- so a later lookup by the lowercase (on-chain)
    form finds this row."""
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=5.0, purpose="p", db=db)
        mixed = "0xAbCdEf0000000000000000000000000000000000000000000000000003"
        assert await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash=mixed, db=db) is True

        row = await db.fetch_one(
            "SELECT transaction_hash FROM x402_payment_requests WHERE id = ?",
            (inv["request_id"],))
        assert row["transaction_hash"] == mixed.lower()
    finally:
        await db.close()


# --- transaction_hash_already_settled + get_payment_request_by_tx_hash ----

@pytest.mark.asyncio
async def test_transaction_hash_already_settled_case_insensitive(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=1.0, purpose="p", db=db)
        mixed = "0xCASE0000000000000000000000000000000000000000000000000004"
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash=mixed, db=db)

        assert await invoicing.transaction_hash_already_settled(mixed, db=db) is True
        assert await invoicing.transaction_hash_already_settled(mixed.lower(), db=db) is True
        assert await invoicing.transaction_hash_already_settled(mixed.upper(), db=db) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_payment_request_by_tx_hash_case_insensitive(tmp_path):
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=1.0, purpose="p", db=db)
        mixed = "0xLookup000000000000000000000000000000000000000000000000005"
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash=mixed, db=db)

        by_mixed = await invoicing.get_payment_request_by_tx_hash(mixed, db=db)
        by_lower = await invoicing.get_payment_request_by_tx_hash(mixed.lower(), db=db)
        by_upper = await invoicing.get_payment_request_by_tx_hash(mixed.upper(), db=db)
        assert by_mixed and by_mixed["request_id"] == inv["request_id"]
        assert by_lower and by_lower["request_id"] == inv["request_id"]
        assert by_upper and by_upper["request_id"] == inv["request_id"]
        # stored canonically -- the returned row reflects the lowercase form
        assert by_mixed["transaction_hash"] == mixed.lower()
    finally:
        await db.close()


# --- revert_stale_settling sweep (:711 in the brief) -----------------------

@pytest.mark.asyncio
async def test_revert_stale_settling_recognizes_case_variant_as_already_settled(tmp_path):
    """A row stranded in 'settling' carrying a transaction_hash that matches
    (case-insensitively) a hash that already settled ANOTHER invoice must
    NOT be resurrected to 'pending' -- it is genuinely settled elsewhere,
    just under a different-cased stamp."""
    db = await _setup_db(tmp_path)
    try:
        inv_settled = await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=2.0, purpose="settled", db=db)
        mixed = "0xStale0000000000000000000000000000000000000000000000000006"
        await invoicing.settle_payment_request(
            inv_settled["request_id"], transaction_hash=mixed, db=db)

        # A second invoice stranded in 'settling', stamped with the SAME tx
        # under a DIFFERENT case (simulates a legacy pre-fix write that
        # bypassed settle_payment_request's normalization -- an UPDATE
        # straight to raw SQL, exactly like an old unnormalized row).
        # Stored raw-mixed-case so it's a DISTINCT string from inv_settled's
        # (already-lowercase) stamp -- the unique index only constrains raw
        # string identity, so this does not collide at the DB level, which
        # is exactly the bug precondition this reaper guard must still catch
        # via NORMALIZED comparison.
        inv_stranded = await invoicing.create_payment_request(
            user_id="rob", session_id="s2", amount_usd=3.0, purpose="stranded", db=db)
        await db.execute(
            "UPDATE x402_payment_requests SET status='settling', "
            "transaction_hash=?, updated_at=datetime('now', '-700 seconds') WHERE id=?",
            (mixed, inv_stranded["request_id"]),
        )

        reverted = await invoicing.revert_stale_settling(max_age_seconds=600, db=db)
        reverted_ids = [r["request_id"] for r in reverted]
        assert inv_stranded["request_id"] not in reverted_ids

        row = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?",
            (inv_stranded["request_id"],))
        assert row["status"] == "settling"  # left alone, not resurrected to pending
    finally:
        await db.close()


# --- Closest-to-the-real-bug proof: the settlement watcher's own loop -----

@pytest.mark.asyncio
async def test_watcher_replay_guard_case_insensitive_end_to_end(tmp_path, monkeypatch):
    """The exact M1 narrative, exercised at the layer the real bug lived in
    (SettlementWatcher._settle_or_flag): the facilitator settles invoice A
    with a mixed-case hash; a later on-chain scan re-observes the SAME
    transfer in lowercase form (eth_getLogs's native casing) and must be
    refused for invoice B, a different pending invoice at the same amount."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    db = await _setup_db(tmp_path)
    try:
        inv_a = await invoicing.create_payment_request(
            user_id="rob", session_id="s_a", amount_usd=5.0, purpose="a", db=db)
        mixed = "0xReplayWatcher00000000000000000000000000000000000000000007"
        await invoicing.settle_payment_request(
            inv_a["request_id"], transaction_hash=mixed, db=db)

        inv_b = await invoicing.create_payment_request(
            user_id="rob", session_id="s_b", amount_usd=5.0, purpose="b", db=db)

        class _WakeAgent:
            pass

        watcher = SettlementWatcher(_WakeAgent(), db=db)
        # eth_getLogs's native casing: the on-chain scanner produces this
        # ALWAYS lowercase (onchain_probe.py). This is the SAME transfer
        # that settled inv_a, re-observed on a later scan.
        transfer = {"tx_hash": mixed.lower(), "from": "0xPayerReplay",
                    "amount_usd": 5.0, "block": 999}
        settled, unmatched = await watcher._settle_or_flag([transfer], TREASURY.lower())

        assert settled == 0
        assert unmatched == 0  # a replay is neither a new settlement nor unmatched
        row_b = await db.fetch_one(
            "SELECT status FROM x402_payment_requests WHERE id = ?",
            (inv_b["request_id"],))
        assert row_b["status"] == "pending"  # NOT settled by the replayed transfer
    finally:
        await db.close()
