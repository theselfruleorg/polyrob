"""Held x402 settlements: release ONLY on a definite answer (DEFI-2).

A submitted invoice whose facilitator outcome is unknown stays in 'settling'.
Two answers reopen it: the facilitator's explicit refusal, or the chain showing
the submitted EIP-3009 authorization unused. Age alone never does.
"""
from typing import Any, Dict, List

from modules.x402._db import resolve_db as _resolve_db


async def reopen_rejected_settlement(request_id: str, nonce: str, *, db=None) -> bool:
    """Reopen an invoice whose submitted authorization DEFINITELY did not pay.

    Two callers only: the facilitator answered an explicit ``success: false``
    with no transaction, or the asset contract's ``authorizationState`` shows
    the submitted nonce unused after the hold window. The update is keyed on
    the submitted nonce, so a later, different submission is never released by
    a stale verdict. An UNKNOWN outcome never comes here — it stays held."""
    database = await _resolve_db(db)
    if database is None or not nonce:
        return False
    cur = await database.execute(
        "UPDATE x402_payment_requests SET status = 'pending', "
        "metadata = json_set(json_remove(COALESCE(metadata, '{}'), "
        "'$.facilitator_submitted', '$.authorization'), '$.last_rejected_nonce', ?), "
        "updated_at = datetime('now') "
        "WHERE id = ? AND status = 'settling' "
        "AND COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=1 "
        "AND lower(json_extract(metadata, '$.authorization.nonce')) = ?",
        (nonce.lower(), request_id, nonce.lower()))
    return bool(getattr(cur, "rowcount", 0))


async def held_submissions(*, min_age_seconds: int = 1800, db=None
                           ) -> List[Dict[str, Any]]:
    """Invoices held in 'settling' after a submission with an unknown outcome,
    older than ``min_age_seconds`` — the rows an on-chain check may resolve."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settling' "
        "AND COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=1 "
        "AND json_extract(metadata, '$.authorization.nonce') IS NOT NULL "
        "AND COALESCE(json_extract(metadata, '$.kind'), '') != 'machine_payment' "
        "AND updated_at <= datetime('now', ?)",
        (f"-{int(max(0, min_age_seconds))} seconds",))
    return [dict(r) for r in rows or []]
