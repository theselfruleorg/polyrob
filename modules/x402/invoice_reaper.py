"""Exits for the x402 invoice states that can stall (098 S6: MO-02, MO-23).

* A `/pay` CLAIM (``settling``, ``claim_lane='facilitator'``, never submitted)
  older than :data:`CLAIM_MAX_AGE_SEC` is released to ``pending``: no external
  party acted on it, so the release is always safe.
* A SUBMISSION is never released on age. The chain resolver releases or
  completes it; when it cannot decide, the row escalates to the owner ONCE.
* A legacy ``settling`` row (no lane, never marked submitted) looks the same as
  a pre-marker `/pay` submission, so it is never released either: it is listed
  (``polyrob owner invoices --stuck``) and escalated to the owner ONCE (owner
  decision Q2). The owner settles it, or reopens it with ``polyrob owner
  reopen <id> --confirm``.
"""
import logging
import time
from typing import Any, Dict, List, Optional

from core.state.sql import acas_transition
from modules.x402._db import resolve_db as _resolve_db
from modules.x402.invoice_status import X402_INVOICE, sql_now

logger = logging.getLogger(__name__)

#: `/pay` holds its claim only across the verify call (maxTimeoutSeconds=300)
#: and marks the submission BEFORE the facilitator settle call.
CLAIM_MAX_AGE_SEC = 600
#: A submission with no resolver answer for this long goes to the owner (MO-23).
SUBMITTED_MAX_AGE_SEC = 24 * 3600

_UNSUBMITTED = "COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=0"
_SUBMITTED = "COALESCE(json_extract(metadata, '$.facilitator_submitted'), 0)=1"


def _meta(row: Dict[str, Any]) -> dict:
    from modules.x402.invoicing import _row_metadata
    return _row_metadata(row)


def _brief(row: Dict[str, Any], **extra) -> Dict[str, Any]:
    meta = _meta(row)
    return {"request_id": row["id"], "amount_usd": row.get("amount_usd"),
            "user_id": meta.get("tenant_id") or row.get("user_id") or "",
            "session_id": meta.get("session_id") or "",
            "purpose": meta.get("purpose") or "", "kind": meta.get("kind") or "", **extra}


async def reap_unsubmitted_claims(*, max_age_seconds: int = CLAIM_MAX_AGE_SEC, db=None,
                                  now: Optional[float] = None) -> List[Dict[str, Any]]:
    """``settling`` (a `/pay` claim that never reached the facilitator) older
    than ``max_age_seconds`` → ``pending``. One guarded write per row, keyed on
    the claim time it read, so a fresh claim of the same row is never released."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = int((time.time() if now is None else now) - max(0, int(max_age_seconds)))
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settling' "
        f"AND claim_lane = 'facilitator' AND {_UNSUBMITTED} "
        "AND claimed_at IS NOT NULL AND claimed_at < ?", (cutoff,))
    out = []
    for row in rows or []:
        row = dict(row)
        if await acas_transition(
                database, "x402_payment_requests", key="id = ?", key_params=(row["id"],),
                machine=X402_INVOICE, to="pending", from_states=("settling",),
                set_cols={"claim_lane": None, "claimed_at": None, "updated_at": sql_now()},
                where_extra=f"claim_lane = 'facilitator' AND claimed_at = ? AND {_UNSUBMITTED}",
                where_params=(row["claimed_at"],)):
            out.append(_brief(row, reason="claim_unsubmitted"))
    return out


async def stuck_settling(*, db=None, tenant: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every ``settling`` row the claim reaper will NOT release: legacy claims
    with no lane, and submissions (the owner's ``--stuck`` listing)."""
    database = await _resolve_db(db)
    if database is None:
        return []
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settling' "
        f"AND NOT (COALESCE(claim_lane, '') = 'facilitator' AND {_UNSUBMITTED}) ORDER BY created_at")
    out = []
    for row in rows or []:
        row = dict(row)
        meta = _meta(row)
        item = _brief(row, submitted=bool(meta.get("facilitator_submitted")),
                      kind=meta.get("kind") or "", held_reason=meta.get("held_reason") or "",
                      escalated_at=meta.get("escalated_at"), created_at=row.get("created_at"),
                      updated_at=row.get("updated_at"))
        if tenant and item["user_id"] != tenant:
            continue
        out.append(item)
    return out


async def escalate_once(request_id: str, reason: str, *, db=None,
                        status: str = "settling") -> bool:
    """Stamp ``$.held_reason`` + ``$.escalated_at`` on a ``status`` row that
    has none yet. True only for the ONE caller that stamped it (it sends the
    single owner notice); the status does not move."""
    database = await _resolve_db(db)
    if database is None:
        return False
    cur = await database.execute(
        "UPDATE x402_payment_requests SET metadata = json_set(COALESCE(metadata, '{}'), "
        "'$.held_reason', ?, '$.escalated_at', ?) "
        "WHERE id = ? AND status = ? "
        "AND json_extract(metadata, '$.escalated_at') IS NULL",
        (str(reason), int(time.time()), request_id, status))
    return bool(getattr(cur, "rowcount", 0))


#: A ``settled_no_tx`` row with no proof after this long goes to the owner (MO-21).
UNPROVEN_MAX_AGE_SEC = 24 * 3600


async def escalate_unproven(*, db=None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """MO-21: a payment recorded without an on-chain tx (``settled_no_tx``)
    that is still unproven after 24 h is returned ONCE for an owner notice. It
    is not income until a tx completes it (``polyrob owner settle <id>
    --tx-hash``) or the owner marks it ``refund_due``/refunded."""
    database = await _resolve_db(db)
    if database is None:
        return []
    cutoff = int((time.time() if now is None else now) - UNPROVEN_MAX_AGE_SEC)
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settled_no_tx' "
        "AND json_extract(metadata, '$.escalated_at') IS NULL "
        "AND CAST(strftime('%s', created_at) AS INTEGER) < ?", (cutoff,))
    out = []
    for row in rows or []:
        row = dict(row)
        if await escalate_once(row["id"], "no_tx_unproven", db=database,
                               status="settled_no_tx"):
            out.append(_brief(row, reason="no_tx_unproven", authorization_used=bool(
                _meta(row).get("authorization_used"))))
    return out


#: How often one ``settled_no_tx`` row's authorization is read on chain again.
NO_TX_RECHECK_SEC = 3600
_ADDR = r"0x[0-9a-fA-F]{40}"
_NONCE = r"0x[0-9a-fA-F]{64}"


async def check_no_tx_authorizations(call_for, *, db=None,
                                     now: Optional[float] = None) -> List[Dict[str, Any]]:
    """098 close-out MO-21: read the EIP-3009 ``authorizationState(payer,
    nonce)`` of each ``settled_no_tx`` row on the asset contract, at most once
    per :data:`NO_TX_RECHECK_SEC`. ``call_for(chain)`` returns a verified
    JSON-RPC ``call`` or None (the settlement watcher's ``_held_call``).

    ``used`` stamps ``$.authorization_used`` = 1: the chain proves the payer's
    authorization was consumed. The row stays ``settled_no_tx`` — it is still
    NOT income: a ``cancelAuthorization`` also marks a nonce used, so only a tx
    (``polyrob owner settle <id> --tx-hash``) completes it. Not used, no data,
    or an RPC error: the row stays unproven (fail-open). Returns the rows that
    became ``authorization_used`` on this pass."""
    import re
    database = await _resolve_db(db)
    if database is None:
        return []
    t = int(time.time() if now is None else now)
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settled_no_tx' "
        "AND COALESCE(json_extract(metadata, '$.authorization_used'), 0) = 0 "
        "AND COALESCE(json_extract(metadata, '$.authorization_checked_at'), 0) < ?",
        (t - NO_TX_RECHECK_SEC,))
    out = []
    for row in rows or []:
        row = dict(row)
        auth = _meta(row).get("authorization") or {}
        payer = str(auth.get("payer") or row.get("payer_address") or "")
        nonce = str(auth.get("nonce") or row.get("nonce") or "")
        asset = str(auth.get("asset") or row.get("asset_address") or "")
        chain = str(auth.get("network") or row.get("chain") or "")
        if not (re.fullmatch(_ADDR, payer) and re.fullmatch(_NONCE, nonce)
                and re.fullmatch(_ADDR, asset)):
            continue   # nothing to ask the chain: stays unproven
        try:
            call = await call_for(chain)
            if call is None:
                continue
            data = "0xe94a0102" + payer[2:].lower().rjust(64, "0") + nonce[2:].lower()
            import asyncio
            raw = await asyncio.to_thread(call, "eth_call", [{"to": asset, "data": data}, "latest"])
            used = int(str(raw), 16) != 0
        except Exception:
            logger.warning("x402 %s: authorizationState read failed; still unproven",
                           row.get("id"), exc_info=True)
            continue
        cur = await database.execute(
            "UPDATE x402_payment_requests SET metadata = json_set(COALESCE(metadata, '{}'), "
            "'$.authorization_used', ?, '$.authorization_checked_at', ?) "
            "WHERE id = ? AND status = 'settled_no_tx'", (1 if used else 0, t, row["id"]))
        if used and getattr(cur, "rowcount", 0):
            out.append(_brief(row, reason="no_tx_authorization_used"))
    return out


async def escalate_stalled(*, db=None, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Rows that need the owner, each returned ONCE (S6):

    * ``settling_unknown`` — a legacy claim with no lane, never marked
      submitted (owner decision Q2: listed, never released on age);
    * ``submitted_stalled`` — a submission older than 24 h that the resolver
      has neither completed nor released (MO-23)."""
    database = await _resolve_db(db)
    if database is None:
        return []
    stale = int((time.time() if now is None else now) - SUBMITTED_MAX_AGE_SEC)
    rows = await database.fetch_all(
        "SELECT * FROM x402_payment_requests WHERE status = 'settling' "
        "AND json_extract(metadata, '$.escalated_at') IS NULL AND ("
        f"(claim_lane IS NULL AND {_UNSUBMITTED}) OR "
        f"({_SUBMITTED} AND COALESCE(claimed_at, CAST(strftime('%s', created_at) AS INTEGER)) < ?))",
        (stale,))
    out = []
    for row in rows or []:
        row = dict(row)
        reason = ("submitted_stalled" if _meta(row).get("facilitator_submitted")
                  else "settling_unknown")
        if await escalate_once(row["id"], reason, db=database):
            out.append(_brief(row, reason=reason))
    return out


async def reopen_claim(request_id: str, *, db=None) -> bool:
    """Owner-attested ``settling → pending`` for a row that was never marked
    submitted (a legacy or stranded claim). A submission is refused: only a
    definite chain or facilitator answer may release it."""
    database = await _resolve_db(db)
    if database is None:
        return False
    return await acas_transition(
        database, "x402_payment_requests", key="id = ?", key_params=(request_id,),
        machine=X402_INVOICE, to="pending", from_states=("settling",),
        set_cols={"claim_lane": None, "claimed_at": None, "updated_at": sql_now()},
        where_extra=_UNSUBMITTED)


async def resolve_held(request_id: str, *, paid: bool, db=None,
                       now: Optional[float] = None) -> bool:
    """098 review (O-03): the owner's decision on a row no reaper can decide —
    a SUBMITTED ``settling`` row or an unproven ``settled_no_tx`` row.

    * ``paid=False`` — the chain shows no payment: ``-> expired`` (never income).
      Refused while the payer's authorization can still be used (its deadline
      has not passed), so a late facilitator settle cannot follow a void.
    * ``paid=True`` — the payer was charged and nothing is proven delivered or
      received: ``-> refund_due`` (the obligation the owner then refunds;
      ``owner refunded`` closes it).
    An unsubmitted claim is ``owner reopen``'s, not this verb's."""
    database = await _resolve_db(db)
    if database is None:
        return False
    t = int(time.time() if now is None else now)
    held = f"(status = 'settled_no_tx' OR (status = 'settling' AND {_SUBMITTED}))"
    if paid:
        return await acas_transition(
            database, "x402_payment_requests", key="id = ?", key_params=(request_id,),
            machine=X402_INVOICE, to="refund_due", from_states=("settling", "settled_no_tx"),
            set_cols={"updated_at": sql_now()}, where_extra=held)
    return await acas_transition(
        database, "x402_payment_requests", key="id = ?", key_params=(request_id,),
        machine=X402_INVOICE, to="expired", from_states=("settling", "settled_no_tx"),
        set_cols={"updated_at": sql_now()},
        where_extra=f"{held} AND COALESCE(deadline, 0) < ?", where_params=(t,))
