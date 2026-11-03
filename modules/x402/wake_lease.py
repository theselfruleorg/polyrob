"""The settlement/expiry WAKE as a lease, not a one-shot latch (098 W1 MO-04).

``metadata.wake_delivered`` used to flip to true at the CLAIM, before the
notice ran: one exception or crash between claim and notify lost the
settlement notice, the session wake or the paid room action for ever. Now:

    none ──claim_wake──▶ claimed ──notify returned──▶ delivered
                            │ older than WAKE_CLAIM_MAX_AGE_SEC
                            └──reap_stale_wake_claims──▶ none (retry), or after
                               WAKE_MAX_ATTEMPTS: delivered + ONE owner notice

``wake_delivered`` is still written in step (true from the claim on) so every
old reader keeps its meaning. A legacy row with ``wake_delivered=true`` and no
``wake_state`` reads as delivered.
"""
import logging
import time
from typing import Any, Dict, List, Optional

from modules.x402._db import resolve_db as _resolve_db

logger = logging.getLogger(__name__)

WAKE_CLAIM_MAX_AGE_SEC = 900
WAKE_MAX_ATTEMPTS = 3


async def mark_delivered(request_id: str, *, db=None) -> bool:
    """``claimed → delivered`` after the notice returned."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        "UPDATE x402_payment_requests SET metadata = json_set(metadata, "
        "'$.wake_state', 'delivered') WHERE id = ? "
        "AND json_extract(metadata, '$.wake_state') = 'claimed'", (request_id,))
    return bool(getattr(cur, "rowcount", 0))


async def reap_stale_wake_claims(*, db=None, now: Optional[float] = None,
                                 max_age_seconds: int = WAKE_CLAIM_MAX_AGE_SEC
                                 ) -> List[Dict[str, Any]]:
    """Re-open every wake claimed longer ago than ``max_age_seconds`` (its
    notice raised or its process died). After :data:`WAKE_MAX_ATTEMPTS` the
    row is closed as delivered and returned, so the caller tells the owner
    ONCE. Each move is guarded on the claim time it read."""
    database = await _resolve_db(db)
    if database is None:
        return []
    now = time.time() if now is None else float(now)
    rows = await database.fetch_all(
        "SELECT id, user_id, amount_usd, metadata FROM x402_payment_requests "
        "WHERE json_extract(metadata, '$.wake_state') = 'claimed' "
        "AND COALESCE(json_extract(metadata, '$.wake_claimed_at'), 0) < ?",
        (int(now - max_age_seconds),))
    failed = []
    from modules.x402.invoicing import _emit, _row_metadata
    for row in rows or []:
        row = dict(row)
        meta = _row_metadata(row)
        attempts = int(meta.get("wake_attempts") or 0) + 1
        give_up = attempts >= WAKE_MAX_ATTEMPTS
        cur = await database.execute(
            "UPDATE x402_payment_requests SET metadata = json_set(metadata, "
            "'$.wake_state', ?, '$.wake_attempts', ?, '$.wake_delivered', json(?)) "
            "WHERE id = ? AND json_extract(metadata, '$.wake_state') = 'claimed' "
            "AND json_extract(metadata, '$.wake_claimed_at') IS ?",
            ("delivered" if give_up else "none", attempts,
             "true" if give_up else "false", row["id"], meta.get("wake_claimed_at")))
        if not getattr(cur, "rowcount", 0):
            continue
        user = meta.get("tenant_id") or row.get("user_id") or ""
        _emit("payment_wake_failed" if give_up else "payment_wake_retried", user_id=user,
              session_id=meta.get("session_id") or "",
              attrs={"request_id": row["id"], "attempts": attempts})
        if give_up:
            failed.append({"request_id": row["id"], "user_id": user,
                           "amount_usd": row.get("amount_usd"), "kind": meta.get("kind") or ""})
    return failed
