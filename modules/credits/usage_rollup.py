"""Tenant-scoped usage rollup — the metering→invoice bridge (Task 13, Phase 3 R3).

`LLMUsageTracker.get_session_breakdown` (``modules/credits/usage_tracker.py``)
is the only existing per-session usage aggregate, and it is NOT tenant-scoped:
it filters by ``session_id`` alone, with no ``user_id`` clause — any caller
holding a session_id (e.g. a stray reference, a forged/leaked id) can read
ANOTHER tenant's usage (G-29, the tenant hole this module fixes). It is kept
for back-compat (existing callers, its richer by-resource-type shape); NEW
callers — starting with the ``usage_summary`` agent action — use
:func:`usage_rollup` instead, which REQUIRES ``user_id`` and only optionally
narrows by ``session_id``/``since``.

This module is a pure READ + a pure SUGGESTION-builder. It never creates a
payment request: :func:`build_invoice_draft` returns an x402_request-shaped
dict the agent must still fire itself via the (approval-gated, Task 9)
``x402_request`` action — the bridge is a proposal, never an autonomous send.
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

_ZERO_TOTALS = {"api_cost_usd": 0.0, "credits": 0.0, "calls": 0}


# ---------------------------------------------------------------------------
# Flags — mirrors the plain-getter style of modules/x402/invoicing.py
# (x402_invoicing_enabled/invoice_max_usd), NOT agents/task/constants.py's
# AutonomyConfig/_SAFE_LOCAL_FLAGS machinery: this is an explicit billing
# feature that must NEVER auto-flip on under POLYROB_LOCAL (per brief).
# ---------------------------------------------------------------------------

def usage_invoice_bridge_enabled() -> bool:
    """USAGE_INVOICE_BRIDGE_ENABLED — gates the `usage_summary` agent action
    (default OFF; deliberately absent from _SAFE_LOCAL_FLAGS)."""
    from core.env import bool_env
    return bool_env("USAGE_INVOICE_BRIDGE_ENABLED", False)


def usage_invoice_markup() -> float:
    """USAGE_INVOICE_MARKUP — multiplier applied to measured api_cost_usd when
    proposing an invoice draft (default 1.0 = passthrough, no markup). A
    DISTINCT flag from PRICING_MARKUP (modules/credits/pricing.py): that one
    prices internal PLATFORM credit-billing; this one prices an OUTBOUND
    agent-to-payer invoice for measured usage — different money flow,
    deliberately not reusing/duplicating the credits-charged rounding logic
    there (this bridge deals in USD, not credits)."""
    try:
        return float(os.getenv("USAGE_INVOICE_MARKUP", "1.0"))
    except ValueError:
        return 1.0


async def _resolve_db(db=None):
    if db is not None:
        return db
    from core.container import DependencyContainer
    return DependencyContainer.get_instance().get_service("database_manager")


def _since_str(since: Optional[Union[str, datetime]]) -> Optional[str]:
    if since is None:
        return None
    if isinstance(since, datetime):
        return since.strftime("%Y-%m-%d %H:%M:%S")
    since = str(since).strip()
    return since or None


async def usage_rollup(
    user_id: str,
    session_id: Optional[str] = None,
    since: Optional[Union[str, datetime]] = None,
    *, db=None,
) -> Dict[str, Any]:
    """Tenant-scoped SUM/COUNT over ``usage_records`` (fixes G-29).

    ``user_id`` is REQUIRED for a non-zero result — an anonymous/empty tenant
    gets the zero rollup back (never a cross-tenant/all-tenants read), the
    same fail-safe direction as ``unified_ledger.build_ledger``'s refusal.
    Narrows further by an optional ``session_id`` and/or a ``since`` floor
    (compared lexicographically against the ``timestamp`` column — pass an
    ISO-ish ``YYYY-MM-DD[ HH:MM:SS]`` string or a ``datetime``).

    When ``session_id`` is not given, ALSO returns a best-effort per-session
    ``by_session`` breakdown (omitted entirely on any error, including a
    partial failure after the main totals succeeded — fail-open means the
    caller gets an honest all-zero rollup, never a half-populated one).

    Fail-open to zeros on ANY error (missing db service, query fault, schema
    drift) — a read-only usage query must never crash a caller.
    """
    result: Dict[str, Any] = {
        "user_id": user_id, "session_id": session_id, "since": _since_str(since),
        **_ZERO_TOTALS,
    }
    if not user_id:
        return result
    try:
        database = await _resolve_db(db)
        if database is None:
            return result
        where: List[str] = ["user_id = ?"]
        params: List[Any] = [user_id]
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        since_s = _since_str(since)
        if since_s:
            where.append("timestamp >= ?")
            params.append(since_s)
        clause = " AND ".join(where)

        row = await database.fetch_one(
            f"""SELECT COALESCE(SUM(api_cost_usd), 0) AS api_usd,
                       COALESCE(SUM(cost), 0) AS credits,
                       COUNT(*) AS calls
                FROM usage_records
                WHERE {clause}""",
            tuple(params),
        )
        totals = {
            "api_cost_usd": round(float(row.get("api_usd") or 0), 6) if row else 0.0,
            "credits": round(float(row.get("credits") or 0), 4) if row else 0.0,
            "calls": int(row.get("calls") or 0) if row else 0,
        }

        by_session: Optional[List[Dict[str, Any]]] = None
        if not session_id:
            rows = await database.fetch_all(
                f"""SELECT session_id,
                           COALESCE(SUM(api_cost_usd), 0) AS api_usd,
                           COALESCE(SUM(cost), 0) AS credits,
                           COUNT(*) AS calls
                    FROM usage_records
                    WHERE {clause}
                    GROUP BY session_id
                    ORDER BY api_usd DESC""",
                tuple(params),
            )
            by_session = [
                {
                    "session_id": r.get("session_id"),
                    "api_cost_usd": round(float(r.get("api_usd") or 0), 6),
                    "credits": round(float(r.get("credits") or 0), 4),
                    "calls": int(r.get("calls") or 0),
                }
                for r in (rows or [])
            ]

        result.update(totals)
        if by_session is not None:
            result["by_session"] = by_session
        return result
    except Exception:
        logger.debug("usage_rollup: query failed, returning zeros", exc_info=True)
        result.update(_ZERO_TOTALS)
        result.pop("by_session", None)
        return result


def _period_label(session_id: Optional[str], since: Optional[str]) -> str:
    if session_id:
        return f"session {session_id}"
    if since:
        return f"since {since}"
    return "all-time"


def build_invoice_draft(rollup: Dict[str, Any], *, purpose: Optional[str] = None) -> Dict[str, Any]:
    """Turn a :func:`usage_rollup` result into an x402_request-shaped
    SUGGESTION — never creates anything. The agent must still call the
    separate, approval-gated ``x402_request`` action itself to actually mint
    a payment request; this function does not import/call
    ``create_payment_request`` at all.

    ``amount_usd = rollup['api_cost_usd'] * USAGE_INVOICE_MARKUP`` (default
    1.0 = passthrough). Respects ``X402_INVOICE_MAX_USD`` (reused from
    ``modules/x402/invoicing.py`` — not duplicated): an over-cap amount is
    returned UNCLAMPED with ``over_cap=True`` so the agent sees the real
    number and can narrow scope / ask the owner to raise the cap, rather than
    a silently-clamped (and therefore wrong) invoice amount.
    """
    from modules.x402.invoicing import invoice_max_usd

    markup = usage_invoice_markup()
    api_cost = float(rollup.get("api_cost_usd") or 0.0)
    amount_usd = round(api_cost * markup, 6)
    period = _period_label(rollup.get("session_id"), rollup.get("since"))
    cap = invoice_max_usd()
    over_cap = amount_usd > cap

    if over_cap:
        note = (
            f"amount ${amount_usd:.2f} exceeds the invoice ceiling ${cap:.2f} "
            "(X402_INVOICE_MAX_USD) — narrow the scope (session/since) or ask "
            "the owner to raise the cap before invoicing; NOT auto-clamped."
        )
    else:
        note = (
            "suggestion only, NEVER auto-sent — call x402_request(amount_usd=..., "
            "purpose=...) yourself to actually create this invoice."
        )

    return {
        "amount_usd": amount_usd,
        "markup": markup,
        "purpose": purpose or f"usage {period} summary",
        "period": period,
        "cap_usd": cap,
        "over_cap": over_cap,
        "note": note,
    }
