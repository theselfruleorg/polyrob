"""
Database Schema Version 1.13.0 — billing-failure resolution states (098 W1 MO-07).

`billing_failures.status` moved `pending → resolved` with one unguarded write
AFTER the money step, so two admin requests both charged the user, and the
kind of resolution lived only in `resolution_notes` text. The resolver is now a
machine (`modules/credits/billing_failures.py`): `pending|hold_unresolved →
resolving → charged|written_off|refunded`.

Adds `resolution`, `resolving_by`, `resolving_at`, and a UNIQUE partial index on
`credit_transactions(reason)` for `billing-failure:%` (a charge lands once).
Backfill: a legacy `resolved` row whose notes start with a known kind gets that
kind as its status and `resolution`; any other `resolved` row stays `resolved`
(a terminal state of the machine). No money moves.
"""
import logging

from modules.credits.billing_failures import KINDS, ensure_columns

logger = logging.getLogger(__name__)

VERSION = "1.13.0"
#: 098 review (O-06): the runners record this migration only when verify() passes.
VERIFY_GATES = True
DESCRIPTION = "billing_failures resolution/resolving_by/resolving_at + one-charge index (098 W1)"


async def upgrade(db, db_manager):
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")
    await ensure_columns(db, logger)
    table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='billing_failures'")
    if table:
        for kind in KINDS:
            await db.execute(
                "UPDATE billing_failures SET status = ?, resolution = ? "
                "WHERE status = 'resolved' AND resolution_notes LIKE ?",
                (kind, kind, f"{kind}:%"))
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))
    return True


async def downgrade(db, db_manager):
    """Version record only (the columns are nullable; the kinds stay readable)."""
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))
    return True


async def verify(db, db_manager):
    try:
        cols = await db.fetch_all("PRAGMA table_info(billing_failures)")
    except Exception:
        cols = None
    if not cols:
        return True
    missing = {"resolution", "resolving_by", "resolving_at"} - {c["name"] for c in cols}
    if missing:
        logger.error("v1.13.0 verify: missing columns %s", sorted(missing))
        return False
    if await db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='credit_transactions'"):
        if not await db.fetch_one("SELECT name FROM sqlite_master WHERE type='index' "
                                  "AND name='idx_credit_trans_billing_failure'"):
            logger.error("v1.13.0 verify: one-charge index idx_credit_trans_billing_failure missing")
            return False
    return True
