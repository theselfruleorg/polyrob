"""Held x402 settlements: release ONLY on a definite answer (DEFI-2).

A submitted invoice whose facilitator outcome is unknown stays in 'settling'.
Two answers reopen it: the facilitator's explicit refusal, or the chain showing
the submitted EIP-3009 authorization unused. Age alone never does. One answer
completes it: the chain showing the recorded facilitator transaction paying the
treasury (the on-chain scan, or the resolver's receipt read).
"""
from typing import Any, Dict, List, Optional

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


def _norm_hash(tx_hash) -> str:
    return str(tx_hash or "").strip().lower()


async def note_facilitator_tx(request_id: str, tx_hash: str, *, db=None) -> bool:
    """Record the facilitator's transaction on a held submission (085 x402in L1).

    `/pay` holds an invoice when the facilitator answers "settled" but the
    receipt does not prove a payment to the treasury yet (unreadable, or not
    mined in the poll window). The hash is what later proves it: the on-chain
    scan settles the invoice when it sees that very transaction pay the
    treasury, and the held-submission resolver re-reads its receipt."""
    database = await _resolve_db(db)
    tx = _norm_hash(tx_hash)
    if database is None or not tx:
        return False
    cur = await database.execute(
        "UPDATE x402_payment_requests SET metadata = json_set("
        "COALESCE(metadata, '{}'), '$.facilitator_tx', ?) "
        "WHERE id = ? AND status = 'settling' "
        "AND COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=1",
        (tx, request_id))
    return bool(getattr(cur, "rowcount", 0))


async def held_by_facilitator_tx(tx_hash: str, *, db=None) -> Optional[Dict[str, Any]]:
    """The held invoice whose recorded facilitator transaction is ``tx_hash``,
    or None."""
    database = await _resolve_db(db)
    tx = _norm_hash(tx_hash)
    if database is None or not tx:
        return None
    row = await database.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE status = 'settling' "
        "AND COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=1 "
        "AND json_extract(metadata, '$.facilitator_tx') = ? LIMIT 1", (tx,))
    return dict(row) if row else None


async def held_machine_payments(*, min_age_seconds: int = 1800, db=None
                                ) -> List[Dict[str, Any]]:
    """Machine-payment attempts held in 'settling' with a recorded facilitator
    tx, older than ``min_age_seconds`` — the rows a receipt read may complete."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settling' "
        "AND COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=1 "
        "AND json_extract(metadata, '$.kind') = 'machine_payment' "
        "AND json_extract(metadata, '$.facilitator_tx') IS NOT NULL "
        "AND updated_at <= datetime('now', ?)",
        (f"-{int(max(0, min_age_seconds))} seconds",))
    return [dict(r) for r in rows or []]


def _is_machine_payment(row: Dict[str, Any]) -> bool:
    import json
    meta = row.get("metadata")
    if not isinstance(meta, dict):
        try:
            meta = json.loads(meta or "{}")
        except (TypeError, ValueError):
            meta = {}
    return (meta or {}).get("kind") == "machine_payment"


async def complete_held(row: Dict[str, Any], tx_hash: str, *, db=None,
                        container: Any = None) -> bool:
    """Complete a held row the chain has PROVEN paid by ``tx_hash``.

    An invoice completes (the settled-notify path wakes its session). A
    machine payment answered 202 "no service was started", so the money was
    taken and nothing was delivered: it is recorded with its tx as
    ``refund_due`` and the owner is told, exactly like a paid request whose
    service failed."""
    from modules.x402 import invoicing
    if not _is_machine_payment(row):
        return await invoicing.settle_payment_request(
            row["id"], transaction_hash=tx_hash, db=db)
    tx = invoicing._norm_tx(tx_hash)
    database = await _resolve_db(db)
    if database is None or not tx:
        return False
    if await invoicing.transaction_hash_already_settled(tx, db=database):
        return False
    try:
        cur = await database.execute(
            "UPDATE x402_payment_requests SET status = 'refund_due', "
            "transaction_hash = ?, completed_at = datetime('now'), "
            "updated_at = datetime('now') WHERE id = ? AND status = 'settling'",
            (tx, row["id"]))
    except Exception:
        return False
    if not getattr(cur, "rowcount", 0):
        return False
    try:
        from modules.x402 import x402_integration as xi
        xi._emit_refund_due(row["id"])
        if container is None:
            from core.container import DependencyContainer
            container = DependencyContainer.get_instance()
        await xi._tell_owner_refund_due(container, row["id"])
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "x402 held machine payment %s: refund-due notice failed", row["id"],
            exc_info=True)
    return True
