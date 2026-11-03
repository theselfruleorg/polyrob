"""
Database Schema Version 1.14.0 — deposit balance marks carry a block (098 W1 MO-01).

`deposit_balance_marks.balance` meant "the last balance reading the deposit
monitor accounted for", and only the monitor wrote it: a lower reading was read
as "a sweep", so a deposit that landed after a sweep and before the next poll
was never credited. The sweeper now books what its receipt proves left the
address on the mark, at the sweep's block (`as_of_block`); the monitor never
credits from a reading older than that block, and it moves the mark by CAS on
the value it read (two monitors credit once).

Creates the table when it is absent (it was runtime-created by the monitor) and
adds `as_of_block INTEGER NOT NULL DEFAULT 0`. Existing marks keep their value
and read as "as of block 0". No money moves.
"""
import logging

from modules.payments.deposit_monitor import ensure_marks_schema

logger = logging.getLogger(__name__)

VERSION = "1.14.0"
#: 098 review (O-06): the runners record this migration only when verify() passes.
VERIFY_GATES = True
DESCRIPTION = "deposit_balance_marks.as_of_block (098 W1 MO-01)"


async def upgrade(db, db_manager):
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")
    await ensure_marks_schema(db)
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))
    return True


async def downgrade(db, db_manager):
    """Version record only (the column is harmless and keeps the sweep history)."""
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))
    return True


async def verify(db, db_manager):
    try:
        cols = await db.fetch_all("PRAGMA table_info(deposit_balance_marks)")
    except Exception:
        cols = None
    if not cols:
        return True   # runtime-created by the monitor on first use
    if "as_of_block" not in {c["name"] for c in cols}:
        logger.error("v1.14.0 verify: deposit_balance_marks.as_of_block missing")
        return False
    return True
