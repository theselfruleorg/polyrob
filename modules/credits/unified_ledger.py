"""Unified agent ledger: one read model over the
three money organs, so "what did I earn and spend this week, on what" is
answerable with evidence.

Legs (each fail-open to zeros — the stores live in different backends and any
may be absent in a given deployment):

- **costs** — LLM/tool self-cost from ``usage_records`` (api_cost_usd is the
  provider truth; credits are the platform-billing view) in the async
  ``database_manager`` store;
- **outbound spend** — wallet payments from the durable telemetry event log's
  ``wallet_spend`` events (``core/wallet/factory._emit_spend_to_event_log``);
- **inbound** — x402 receipts from ``x402_payment_requests`` (settled rows),
  plus the open pipeline (pending agent invoices).

Deliberately read-only and deliberately NOT a new table: `modules/credits`
stays the platform-billing concern; this is the agent's own balance-sheet VIEW
joining what already exists (application-level union — the legs share no DB).
"""
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def _resolve_db(db=None):
    if db is not None:
        return db
    from core.container import DependencyContainer
    return DependencyContainer.get_instance().get_service("database_manager")


async def _costs_leg(database, user_id: str, days: int) -> Dict[str, Any]:
    try:
        row = await database.fetch_one(
            """SELECT COALESCE(SUM(api_cost_usd), 0) AS api_usd,
                      COALESCE(SUM(cost), 0) AS credits,
                      COUNT(*) AS calls
               FROM usage_records
               WHERE user_id = ? AND timestamp >= datetime('now', ?)""",
            (user_id, f"-{int(days)} day"),
        )
        return {
            "llm_api_cost_usd": round(float(row.get("api_usd") or 0), 6) if row else 0.0,
            "credits_spent": round(float(row.get("credits") or 0), 4) if row else 0.0,
            "llm_calls": int(row.get("calls") or 0) if row else 0,
        }
    except Exception:
        logger.debug("ledger: usage_records leg unavailable", exc_info=True)
        return {"llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0}


def _wallet_leg(user_id: str, days: int) -> Dict[str, Any]:
    if not user_id:
        # query(user_id=None) means "no filter" — an empty tenant must NEVER
        # widen into the platform-wide spend aggregate (cross-tenant leak)
        return {"wallet_spend_usd": 0.0, "wallet_payments": 0}
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return {"wallet_spend_usd": 0.0, "wallet_payments": 0}
        events = get_event_log().query(
            kind="wallet_spend", user_id=user_id,
            since_ts=time.time() - days * 86400, limit=1000,
        )
        total = 0.0
        for ev in events or []:
            attrs = ev.get("attrs") or {}
            try:
                total += float(attrs.get("amount_usd") or 0)
            except (TypeError, ValueError):
                continue
        return {"wallet_spend_usd": round(total, 6), "wallet_payments": len(events or [])}
    except Exception:
        logger.debug("ledger: wallet_spend leg unavailable", exc_info=True)
        return {"wallet_spend_usd": 0.0, "wallet_payments": 0}


async def _inbound_leg(database, user_id: str, days: int) -> Dict[str, Any]:
    try:
        tenant_like = f'%"tenant_id": "{user_id}"%'
        settled = await database.fetch_one(
            """SELECT COALESCE(SUM(amount_usd), 0) AS usd, COUNT(*) AS n
               FROM x402_payment_requests
               WHERE (user_id = ? OR metadata LIKE ?)
                 AND status IN ('completed', 'settled_no_tx')
                 AND created_at >= datetime('now', ?)""",
            (user_id, tenant_like, f"-{int(days)} day"),
        )
        pending = await database.fetch_one(
            """SELECT COALESCE(SUM(amount_usd), 0) AS usd, COUNT(*) AS n
               FROM x402_payment_requests
               WHERE (user_id = ? OR metadata LIKE ?) AND status = 'pending'""",
            (user_id, tenant_like),
        )
        return {
            "earned_usd": round(float(settled.get("usd") or 0), 6) if settled else 0.0,
            "settled_payments": int(settled.get("n") or 0) if settled else 0,
            "pending_invoices_usd": round(float(pending.get("usd") or 0), 6) if pending else 0.0,
            "pending_invoices": int(pending.get("n") or 0) if pending else 0,
        }
    except Exception:
        logger.debug("ledger: x402 inbound leg unavailable", exc_info=True)
        return {"earned_usd": 0.0, "settled_payments": 0,
                "pending_invoices_usd": 0.0, "pending_invoices": 0}


async def build_ledger(user_id: str, *, days: int = 7, db=None) -> Dict[str, Any]:
    """The unified balance-sheet view for one tenant over a trailing window.

    Tenant-scoped by contract: an empty user_id is refused (mirrors
    MEMORY_REQUIRE_USER_ID) — anonymity must never widen into an all-tenants view.
    """
    if not user_id:
        raise ValueError("accounting requires an authenticated tenant (empty user_id refused)")
    days = max(1, int(days))
    database = await _resolve_db(db)
    costs = (await _costs_leg(database, user_id, days)) if database is not None else {
        "llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0}
    inbound = (await _inbound_leg(database, user_id, days)) if database is not None else {
        "earned_usd": 0.0, "settled_payments": 0,
        "pending_invoices_usd": 0.0, "pending_invoices": 0}
    wallet = _wallet_leg(user_id, days)
    total_out = costs["llm_api_cost_usd"] + wallet["wallet_spend_usd"]
    return {
        "user_id": user_id,
        "window_days": days,
        **costs,
        **wallet,
        **inbound,
        "total_spend_usd": round(total_out, 6),
        "net_usd": round(inbound["earned_usd"] - total_out, 6),
    }


def format_ledger(ledger: Dict[str, Any]) -> str:
    """Agent/owner-readable rendering of :func:`build_ledger` (pure)."""
    return (
        f"Ledger — last {ledger['window_days']}d (tenant {ledger['user_id'] or '(all)'})\n"
        f"  earned:   ${ledger['earned_usd']:.4f} across {ledger['settled_payments']} settled payment(s)\n"
        f"  pending:  ${ledger['pending_invoices_usd']:.4f} across {ledger['pending_invoices']} open invoice(s)\n"
        f"  spent:    ${ledger['total_spend_usd']:.4f} total — "
        f"${ledger['llm_api_cost_usd']:.4f} LLM/API ({ledger['llm_calls']} calls), "
        f"${ledger['wallet_spend_usd']:.4f} wallet ({ledger['wallet_payments']} payment(s))\n"
        f"  credits:  {ledger['credits_spent']:.2f} platform credits consumed\n"
        f"  net:      ${ledger['net_usd']:+.4f}"
    )
