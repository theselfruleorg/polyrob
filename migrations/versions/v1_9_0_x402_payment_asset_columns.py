"""
Database Schema Version 1.9.0 — x402_payment_requests asset columns
(proposal 046 Phase 0).

Before this, the invoice row could only describe ONE asset: `asset` held the
literal string "usdc" (`modules/x402/invoicing.py`), the on-chain probe assumed
6 decimals, and the settlement match compared a float USD figure treasury-wide
with no asset filter at all. Four nullable columns let a row say WHICH token, at
what precision, and in what RAW amount — which is what makes an exact,
asset-keyed integer match possible.

⚠️ `amount_raw` is TEXT: an 18-decimal amount exceeds SQLite's signed 64-bit
INTEGER range, and an INTEGER column would overflow silently on a large-supply
token.

Existing rows keep `asset = 'usdc'` and NULL asset columns. Every reader treats
a NULL `asset_id` as `usdc-base`, so there is no backfill and no historical row
changes meaning. An invoice outstanding across the deploy stays settleable
(`invoicing.match_pending_invoice` keeps a legacy branch for exactly that).
"""
import logging

from modules.database.x402_tables import add_payment_asset_columns

logger = logging.getLogger(__name__)

VERSION = "1.9.0"
DESCRIPTION = ("x402_payment_requests asset_id/asset_address/asset_decimals/"
               "amount_raw (046 payment assets)")


async def upgrade(db, db_manager):
    """Apply v1.9.0 — add the four asset columns, idempotently.

    Delegates to `add_payment_asset_columns`, shared with
    `X402Tables.create_tables()`, so a FRESH install and an UPGRADED one can
    never disagree about the schema. Tolerant of a DB that has no
    `x402_payment_requests` yet (skips cleanly); re-running on an already-widened
    DB touches nothing.
    """
    logger.info(f"Applying schema: {VERSION} - {DESCRIPTION}")

    await add_payment_asset_columns(db, logger)

    await db.execute("""
        INSERT OR REPLACE INTO schema_versions (version, description, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
    """, (VERSION, DESCRIPTION))

    logger.info(f"Schema {VERSION} applied successfully!")
    return True


async def downgrade(db, db_manager):
    """Downgrade from v1.9.0 — version record only.

    SQLite could not DROP COLUMN before 3.35, and dropping these would destroy
    asset provenance on every 046-era row (which token an invoice was actually
    payable in). The columns are nullable and every reader treats NULL as
    `usdc-base`, so leaving them in place is harmless and reversible in meaning.
    """
    logger.info(f"Downgrading schema: {VERSION}")

    await db.execute("DELETE FROM schema_versions WHERE version = ?", (VERSION,))

    logger.info(f"Schema {VERSION} downgrade complete (columns left in place — "
                f"dropping them would destroy asset provenance)")
    return True


async def verify(db, db_manager):
    """Every asset column is present, or the 046 rail cannot run."""
    try:
        cols = await db.fetch_all("PRAGMA table_info(x402_payment_requests)")
    except Exception:
        cols = None
    if not cols:
        # No table yet; `create_tables` adds the columns when it creates it.
        return True
    names = {c["name"] for c in cols}
    missing = {"asset_id", "asset_address", "asset_decimals", "amount_raw"} - names
    if missing:
        logger.error("v1.9.0 verify: missing columns %s", sorted(missing))
        return False
    return True
