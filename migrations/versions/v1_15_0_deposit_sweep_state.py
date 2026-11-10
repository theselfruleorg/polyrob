"""
Database Schema Version 1.15.0 — deposit sweeps carry a state (098 close-out MO-01).

A treasury sweep of a deposit group was one write after the broadcast
(`swept_at`), so a sweep whose outcome was unknown left no trace and a remote
signer sweep (a tx hash, no receipt) read the same as a proven one.
`crypto_payments.sweep_state` is now an explicit machine
(`modules/payments/sweep_state.py`): unswept -> sweeping -> swept |
swept_unproven | sweep_failed, with `sweep_started_at` (the claim) and
`sweep_escalated_at` (the one owner notice for a sweep stuck in `sweeping`).

Additive columns only. A row swept before this version reads `swept`. No money
moves. The table is runtime-created (AuthTables); absent = nothing to do.
"""
import logging

from modules.payments.sweep_state import ensure_sweep_schema

logger = logging.getLogger(__name__)

VERSION = "1.15.0"
#: 098 review (O-06): the runners record this migration only when verify() passes.
VERIFY_GATES = True
DESCRIPTION = "crypto_payments.sweep_state/sweep_started_at/sweep_escalated_at (098 MO-01)"


async def upgrade(db, db_manager):
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")
    await ensure_sweep_schema(db)
    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))
    return True


async def downgrade(db, db_manager):
    """Version record only (the columns are harmless and keep the sweep history)."""
    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))
    return True


async def verify(db, db_manager):
    try:
        cols = await db.fetch_all("PRAGMA table_info(crypto_payments)")
    except Exception as exc:
        # Unreadable is not absent: the gate fails rather than pass a store it never read.
        logger.error("v1.15.0 verify: could not read crypto_payments: %s", exc)
        return False
    if not cols:
        return True   # runtime-created by AuthTables on first use
    missing = {"sweep_state", "sweep_started_at", "sweep_escalated_at"} - {c["name"] for c in cols}
    if missing:
        logger.error("v1.15.0 verify: crypto_payments missing %s", sorted(missing))
        return False
    return True
