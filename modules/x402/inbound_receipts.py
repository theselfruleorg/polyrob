"""x402 inbound receipts for the unified ledger (067 P5a).

The SQL over ``x402_payment_requests`` that ``modules/credits/unified_ledger``
ran inline, moved here unchanged and registered through the credits hook
``register_inbound_receipts`` (the ``credits.inbound_receipts`` seam). The
credits module no longer names an x402 table; 067 P5c moves this module with
the rest of ``modules/x402`` into the wallet pack, which registers it instead.
"""
from typing import Any, Dict


async def inbound_receipts(database, user_id: str, days: int) -> Dict[str, Any]:
    """Settled income, pending invoices and refunds owed, for one tenant.
    Raises on a read failure (the ledger marks the leg unavailable)."""
    # json_extract (SQLite JSON1, bundled), not `metadata LIKE '%"tenant_id":
    # "<id>"%'` — a LIKE pattern treats `_`/`%` as wildcards, and real tenant
    # ids contain underscores (u_<hex>), so 'u_abc' would also match a
    # lookalike 'uXabc' row on this money query (G-14).
    settled = await database.fetch_one(
        """SELECT COALESCE(SUM(amount_usd), 0) AS usd, COUNT(*) AS n
           FROM x402_payment_requests
           WHERE (user_id = ? OR json_extract(metadata, '$.tenant_id') = ?)
             AND status IN ('completed', 'settled_no_tx')
             AND created_at >= datetime('now', ?)""",
        (user_id, user_id, f"-{int(days)} day"),
    )
    pending = await database.fetch_one(
        """SELECT COALESCE(SUM(amount_usd), 0) AS usd, COUNT(*) AS n
           FROM x402_payment_requests
           WHERE (user_id = ? OR json_extract(metadata, '$.tenant_id') = ?) AND status = 'pending'""",
        (user_id, user_id),
    )
    # D11 (2026-09-21): money we TOOK and must give back is neither income nor
    # pending — it is its own line, or the ledger reads richer than it is.
    refund = await database.fetch_one(
        """SELECT COALESCE(SUM(amount_usd), 0) AS usd, COUNT(*) AS n
           FROM x402_payment_requests
           WHERE (user_id = ? OR json_extract(metadata, '$.tenant_id') = ?) AND status = 'refund_due'""",
        (user_id, user_id),
    )
    return {
        "income_usd": round(float(settled.get("usd") or 0), 6) if settled else 0.0,
        "settled_payments": int(settled.get("n") or 0) if settled else 0,
        "pending_invoices_usd": round(float(pending.get("usd") or 0), 6) if pending else 0.0,
        "pending_invoices": int(pending.get("n") or 0) if pending else 0,
        "refund_due_usd": round(float(refund.get("usd") or 0), 6) if refund else 0.0,
        "refund_due_count": int(refund.get("n") or 0) if refund else 0,
    }


from modules.credits.unified_ledger import register_inbound_receipts  # noqa: E402

register_inbound_receipts(inbound_receipts)
