"""
Database Schema Version 1.8.0 - x402_payment_requests.transaction_hash case
normalization (M1, security audit 2026-08-22).

The on-chain settlement scanner's tx hash comes from `eth_getLogs`
(`modules/x402/onchain_probe.py`, always lowercase — a tx hash, unlike an
address, has no EIP-55 checksum casing). A facilitator's settlement hash
(`modules/x402/middleware.py`, `api/x402_endpoints.py` via
`settle_payment_request`) is stored whatever case it returns. The replay
guard (`modules.x402.invoicing.transaction_hash_already_settled` /
`get_payment_request_by_tx_hash`) compared these as RAW STRINGS: when
`/pay` settled invoice X via the facilitator, that settlement is ITSELF a
USDC transfer into the treasury, so a later on-chain scan re-observes it. If
the two hash strings differed only in case, the guard missed the replay and
`match_pending_invoice_by_amount` went on to settle a DIFFERENT pending
invoice of the same amount from the SAME real payment — a second invoice
marked paid with no money behind it.

The fix (`modules.x402.invoicing._norm_tx`, applied at every store AND
compare site in that module, plus the middleware store call) makes every
NEW write and every compare canonical lowercase. This migration backfills
EXISTING rows so they are found by the now-normalized guard too — a
lowercased query parameter would otherwise miss a mixed-case legacy row
forever.

Collision handling (⚠️ money-critical, read before touching this file):
`transaction_hash` carries a partial UNIQUE index
(`idx_x402_requests_tx_hash_unique`, v1.6.0). If two EXISTING rows differ
only by case, lowering BOTH collides against that index. Since real tx
hashes are 64 hex chars, two distinct stored strings that lower to the SAME
value are — for all practical purposes — the SAME on-chain transaction
recorded twice under different casing: exactly the M1 double-settle
signature, meaning a real double-settle ALREADY happened before this fix
shipped. This migration therefore:
  1. Finds every such case-collision group FIRST (before writing anything).
  2. Logs it loudly (`normalize_tx_hash_case` in
     `modules.database.x402_tables`) — every affected request_id, for owner
     reconciliation.
  3. Leaves those specific rows COMPLETELY untouched (original casing kept)
     — it does NOT guess a winner and merge/null the loser (that decision
     belongs to a human who can check on-chain which settlement is real).
  4. Lowercases every OTHER (non-colliding) mixed-case row.
It never fails the migration and never silently merges.

Self-healing note: `modules.database.x402_tables.X402Tables.create_tables()`
ALSO runs this same backfill (`normalize_tx_hash_case`) on every boot, AFTER
`dedupe_and_create_tx_hash_unique_index` — same precedent as v1.6.0/v1.7.0 —
so this migration is belt-and-suspenders for explicit schema-version
tracking, not the sole application path.

Created: 2026-08-22
"""

import logging

from modules.database.x402_tables import normalize_tx_hash_case

logger = logging.getLogger(__name__)

VERSION = "1.8.0"
DESCRIPTION = "x402_payment_requests.transaction_hash case-normalize backfill (M1 replay-guard fix)"


async def upgrade(db, db_manager):
    """Apply v1.8.0 - lowercase every transaction_hash, collision-safe.

    Delegates to `normalize_tx_hash_case` (shared with
    `X402Tables.create_tables()`), which is tolerant of a DB that doesn't
    have `x402_payment_requests` at all yet (skips cleanly) and detects any
    case-collision set BEFORE writing anything, logging it loudly and
    leaving those rows untouched rather than merging (see module docstring).
    Idempotent: re-running on an already-lowercase DB touches zero rows.
    """
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    await normalize_tx_hash_case(db, logger)

    # Record version
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.8.0 -- a data backfill, not a schema change: there
    is nothing to structurally revert (lowercasing a hex hash loses no
    information and is not destructive), so this only removes the version
    record."""
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete (data backfill left in place -- "
                f"lowercasing a hex hash is not destructive/reversible-worthy)")
    return True


async def verify(db, db_manager):
    """Verify schema was applied correctly: no NON-colliding mixed-case
    transaction_hash rows remain, and the version is recorded. A row left
    mixed-case because it is part of a logged collision group is NOT a
    verification failure -- that is the intended, documented degrade."""

    try:
        table_cols = await db.fetch_all("PRAGMA table_info(x402_payment_requests)")
        if table_cols:
            mixed_case_rows = await db.fetch_all(
                """SELECT id, transaction_hash FROM x402_payment_requests
                   WHERE transaction_hash IS NOT NULL
                     AND transaction_hash <> lower(transaction_hash)"""
            )
            for row in mixed_case_rows or []:
                # A remaining mixed-case row is only legitimate if it is part
                # of a logged case-collision group (>1 row sharing its lower()).
                sibling = await db.fetch_one(
                    """SELECT COUNT(*) AS ct FROM x402_payment_requests
                       WHERE transaction_hash IS NOT NULL
                         AND lower(transaction_hash) = lower(?)""",
                    (row["transaction_hash"],),
                )
                if not sibling or int(sibling["ct"] or 0) < 2:
                    logger.error(
                        "transaction_hash %s (request_id=%s) is still "
                        "mixed-case with no collision sibling -- backfill "
                        "did not run cleanly", row["transaction_hash"], row["id"])
                    return False

        result = await db.fetch_one("""
            SELECT version FROM schema_versions WHERE version = ?
        """, (VERSION,))
        if not result:
            logger.error("Version not recorded in schema_versions")
            return False

        logger.info(f"Verification passed for {VERSION}")
        return True

    except Exception as e:
        logger.error(f"Verification error for {VERSION}: {e}")
        return False
