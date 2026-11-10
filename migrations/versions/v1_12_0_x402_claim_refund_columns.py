"""
Database Schema Version 1.12.0 — x402 invoice state columns (proposal 098 W1).

`settling` used to carry two states that only `metadata.facilitator_submitted`
separated: a local `/pay` CLAIM (no external party has acted — always safe to
release) and a facilitator SUBMISSION (the payer may have paid — never released
on age). `claim_lane` + `claimed_at` let the claim reaper release exactly the
first kind (MO-02). `refunded_at` / `refund_tx_hash` / `refund_note` give a
`refund_due` row its exit, the owner's `refunded` verb (MO-03). `created_block`
is the first block an invoice could be paid in, the on-chain scan seed (MO-06).

Additive and nullable: a legacy row keeps NULL in every column, and no reader
changes its meaning for it. A legacy `settling` row (no `claim_lane`) is never
released on age — it is listed for the owner instead (owner decision Q2).
"""
import logging

from modules.database.x402_tables import _INVOICE_STATE_COLUMNS, add_invoice_state_columns

logger = logging.getLogger(__name__)

VERSION = "1.12.0"
#: 098 review (O-06): the runners record this migration only when verify() passes.
VERIFY_GATES = True
DESCRIPTION = ("x402_payment_requests claim_lane/claimed_at/refunded_at/"
               "refund_tx_hash/refund_note/created_block (098 W1)")


async def upgrade(db, db_manager):
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")
    await add_invoice_state_columns(db, logger)
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))
    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Version record only: the columns are nullable and harmless, and dropping
    them would lose refund records."""
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))
    return True


async def verify(db, db_manager):
    try:
        cols = await db.fetch_all("PRAGMA table_info(x402_payment_requests)")
    except Exception as exc:
        # Unreadable is not absent: the gate fails rather than pass a store it never read.
        logger.error("v1.12.0 verify: could not read x402_payment_requests: %s", exc)
        return False
    if not cols:
        return True
    missing = {n for n, _ in _INVOICE_STATE_COLUMNS} - {c["name"] for c in cols}
    if missing:
        logger.error("v1.12.0 verify: missing columns %s", sorted(missing))
        return False
    return True
