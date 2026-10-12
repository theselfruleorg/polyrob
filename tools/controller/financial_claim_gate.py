"""The send-time gate for financial success claims (proposal 022).

ONE pre-tool-call hook on the Controller, registered fail-closed next to the
wallet authority hook. Every outbound rail passes it because every outbound
rail is an action the Controller dispatches — there is no per-tool copy.

:data:`OUTBOUND_TEXT_FIELDS` is the table of outbound verbs and the param
fields that carry their words, derived from the ``text_fields`` column of the
verb-policy rows (``core/verb_policy_rows.py``; a pack's in its ``pack.toml``). ``tests/unit/tools/controller/
test_financial_claim_gate.py`` enumerates every action of every tool whose
catalog permissions include ``social.post`` / ``email.send`` (plus the
Controller's own ``message`` / ``send_message``) and fails when a text-bearing
verb is missing here — a new outbound verb cannot skip the gate by accident.

The pure detection + matching lives in ``tools.rails.financial_claims``; this
module only reads the two settled-money stores:

- money OUT — the wallet PolicyGate audit log (every booked spend, all venues);
- money IN — settled x402 invoices (``x402_payment_requests`` rows).

Kill switch: ``FINANCIAL_CLAIM_GATE_ENABLED`` (default ON).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.lazy_views import lazy_module_getattr, view
from tools.rails.financial_claims import (
    RECEIVE, SPEND, RecordsRead, SettledRecord, detect_claims, outbound_texts,
    verify_claim,
)

logger = logging.getLogger(__name__)


def _build_outbound_text_fields() -> Dict[str, Tuple[str, ...]]:
    """action name -> the param fields whose text leaves the box: the
    ``text_fields`` column of the verb-policy rows (W4), core and pack rows."""
    from core.verb_policy import ordered_ids_where, policy_for
    return {n: policy_for(n).text_fields for n in ordered_ids_where(text_fields=bool)}


#: action name -> the param fields whose text leaves the box (lazy; a derived
#: view of the ``text_fields`` column, built on first read).
__getattr__ = lazy_module_getattr(__name__, {
    "OUTBOUND_TEXT_FIELDS": _build_outbound_text_fields})


def _epoch(value: Any) -> float:
    """An epoch float from a number or a SQLite ``datetime('now')`` string
    (UTC, ``YYYY-MM-DD HH:MM:SS``). Unparseable -> 0 (outside every window)."""
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


def gate_enabled() -> bool:
    """The owner's ``SECURITY_SETTINGS`` row ``financial_claim_gate`` (on|off,
    default on); the older ``FINANCIAL_CLAIM_GATE_ENABLED=false`` also turns it off."""
    from core.env import bool_env
    from core.security import settings
    return (settings.value("financial_claim_gate") != "off"
            and bool_env("FINANCIAL_CLAIM_GATE_ENABLED", True))


def _spend_records() -> Tuple[List[SettledRecord], Optional[str]]:
    """Booked spends from the wallet audit log. A wallet that is not configured
    has an empty log, which is the truth: this agent cannot have paid."""
    try:
        from core.wallet.factory import get_policy_gate
        gate = get_policy_gate()
        if gate is None:
            return [], None
        # Fold in spends another process (the daemon, a CLI run) booked since
        # this process read the ledger; a false return is an unhealthy sink.
        sync = getattr(gate, "_sync_shared_ledger", None)
        if callable(sync) and sync() is False:
            return [], "the wallet audit log (it is marked unhealthy)"
        out = []
        for e in gate.audit_log:
            try:
                out.append(SettledRecord(
                    direction=SPEND, amount_usd=float(e.get("amount_usd") or 0),
                    ts=_epoch(e.get("ts")), ref=str(e.get("result_ref") or ""),
                    source="wallet audit"))
            except (TypeError, ValueError, AttributeError):
                continue
        return out, None
    except Exception as exc:
        logger.warning("financial-claim gate: wallet audit unreadable: %s", exc)
        return [], "the wallet audit log"


async def _receive_records(user_id: str) -> Tuple[List[SettledRecord], Optional[str]]:
    """Settled invoices for this tenant. No database at all (a bare CLI with no
    invoicing) is an empty store, not an unreadable one — nothing can have
    settled into a table that does not exist."""
    try:
        from modules.x402 import invoicing
        from modules.x402._db import resolve_db
        db = await resolve_db()
        if db is None:
            return [], None
        rows = await invoicing.list_payment_requests(
            user_id=user_id, status="completed", limit=200, db=db)
        out = []
        for r in rows or []:
            from modules.x402.income_chains import is_income_chain
            if not is_income_chain(r.get("chain"), r.get("asset_id")):
                continue
            try:
                out.append(SettledRecord(
                    direction=RECEIVE, amount_usd=float(r.get("amount_usd") or 0),
                    ts=_epoch(r.get("completed_at") or r.get("created_at")),
                    ref=str(r.get("request_id") or ""), source="settled invoices"))
            except (TypeError, ValueError):
                continue
        return out, None
    except Exception as exc:
        logger.warning("financial-claim gate: settled invoices unreadable: %s", exc)
        return [], "the settled invoices"


async def read_settled_records(user_id: str) -> RecordsRead:
    """Both stores. Module-level so a test can monkeypatch it."""
    read = RecordsRead()
    spends, bad = _spend_records()
    read.records.extend(spends)
    if bad:
        read.unreadable.append(bad)
    receipts, bad = await _receive_records(user_id)
    read.records.extend(receipts)
    if bad:
        read.unreadable.append(bad)
    return read


def _record(controller, action_name: str, context: Any, detail: str) -> None:
    try:
        from core.security.refusals import record_refusal
        record_refusal("financial_claim", tool=action_name,
                       user_id=getattr(context, "user_id", "") or getattr(controller, "user_id", ""),
                       session_id=getattr(context, "session_id", "") or "",
                       detail=detail)
    except Exception:
        logger.debug("financial-claim refusal record skipped", exc_info=True)


async def check_outbound(action_name: str, params: Any, user_id: str,
                         context: Any = None, controller: Any = None) -> Optional[str]:
    """``None`` to allow; the named refusal to block. Non-outbound actions and
    outbound text with no financial claim return ``None`` without any read."""
    fields = view(__name__, "OUTBOUND_TEXT_FIELDS").get(str(action_name or ""))
    if not fields or not gate_enabled():
        return None
    text = "\n".join(outbound_texts(params, fields))
    claims = detect_claims(text)
    if not claims:
        return None
    read = await read_settled_records(user_id)
    for claim in claims:
        refusal = verify_claim(claim, read)
        if refusal:
            logger.warning("⛔ %s blocked: unverified financial claim %r", action_name,
                           claim.sentence[:120])
            _record(controller, action_name, context, claim.sentence)
            return refusal
    return None


def make_financial_claim_hook(controller):
    """The Controller pre-tool-call hook (register fail_mode="closed")."""
    async def check(name, params, context):
        uid = getattr(context, "user_id", None) or getattr(controller, "user_id", "") or ""
        return await check_outbound(name, params, str(uid), context, controller)
    return check


__all__ = ["OUTBOUND_TEXT_FIELDS", "check_outbound", "gate_enabled",
           "make_financial_claim_hook", "read_settled_records"]
