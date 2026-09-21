"""Agent ledger: TWO read models, never summed.

- **treasury** — the agent's own money (USDC): income (settled x402), spend
  (wallet outflow), pending invoices, net = income - spend.
- **runtime** — the OWNER's money: what it costs to run the agent (usage_records
  api_cost_usd), window + lifetime. It has no net; there is nothing to net it
  against.

These are different pockets. Summing them produces "net = (agent's income) -
(owner's API bill + agent's outflow)", which subtracts the owner's money from
the agent's and calls the result the agent's net. That was the 2026-07-16 bug.

Balances are display-only network reads, gated behind include_balances (§4.1):
not every provider exposes one, so a balance is never authoritative and never
gates anything. Unknown -> None, never 0.0.

Deliberately read-only and deliberately NOT a new table.

Legs (each fail-open to zeros — the stores live in different backends and any
may be absent in a given deployment):

- **costs** — LLM/tool self-cost from ``usage_records`` (api_cost_usd is the
  provider truth; credits are the platform-billing view) in the async
  ``database_manager`` store;
- **outbound spend** — wallet payments from the durable telemetry event log's
  ``wallet_spend`` events (``core/wallet/factory._emit_spend_to_event_log``);
- **inbound** — x402 receipts from ``x402_payment_requests`` (settled rows),
  plus the open pipeline (pending agent invoices).
"""
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def _resolve_db(db=None):
    if db is not None:
        return db
    from core.container import DependencyContainer
    # 2026-09-21: `get_instance()` RAISES when no container was ever built, so
    # every container-less seat (`polyrob doctor`, the recap, a bare CLI) saw
    # "could not verify: money (ValueError: Configuration required …)" — an
    # unreadable ledger over a store it never tried to open. No container is
    # simply no db here; the legs then render unavailable, never a raise.
    if DependencyContainer._instance is None:
        return None
    return DependencyContainer.get_instance().get_service("database_manager")


def _policy_gate():
    """Seam over ``core.wallet.factory.get_policy_gate()`` — module-level so a
    test can monkeypatch it directly (A39/A6, 043). Fail-open to ``None`` on
    ANY exception (wallet disabled, misconfigured env, no data home yet):
    the caps block this feeds must degrade to all-``None``, never a
    fabricated ``$0.00`` cap."""
    try:
        from core.wallet.factory import get_policy_gate
        return get_policy_gate()
    except ImportError:
        # A base install without the `crypto` extra has no wallet BY DESIGN, so
        # this is not a warn-worthy condition (2026-09-21). It used to warn, and
        # on `polyrob doctor` — a read-only verb — that single warning was enough
        # to materialise `<cwd>/.polyrob/logs/` on a machine that had never run
        # the agent. The caps block degrades to all-`None` either way.
        logger.debug("ledger: no wallet support installed; caps block unavailable")
        return None
    except Exception:
        logger.warning("ledger: policy gate unavailable for caps block", exc_info=True)
        return None


def _caps_block() -> Dict[str, Any]:
    """Display-only wallet policy headroom — configuration, not money, so it
    is its own top-level ``caps`` block (never folded into ``treasury``).
    Every value is ``None`` when the gate can't be read, never ``0`` (a
    ``$0.00`` cap would read as "spending is fully blocked", which is not
    what "we couldn't check" means)."""
    empty = {"daily_cap_usd": None, "daily_used_usd": None,
             "daily_left_usd": None, "per_tx_cap_usd": None}
    gate = _policy_gate()
    if gate is None:
        return empty
    try:
        cap = gate.daily_cap_usd
        used = gate.rolling_24h_spend_usd()
        per_tx = getattr(gate, "per_tx_cap_usd", None)
        left = max(0.0, cap - used) if (cap is not None and used is not None) else None
        return {"daily_cap_usd": cap, "daily_used_usd": used,
                "daily_left_usd": left, "per_tx_cap_usd": per_tx}
    except Exception:
        logger.warning("ledger: policy gate caps unreadable", exc_info=True)
        return empty


async def _costs_leg(database, user_id: str, days: int) -> Dict[str, Any]:
    # H14b (2026-07-15): each leg carries an availability marker so a MISSING /
    # corrupt table ("no data yet") is distinguishable from a genuine $0.00 —
    # the renderers must never present an absent data layer as an honest-looking
    # zero. `costs_available=False` means the usage_records read did not succeed
    # (table absent, DB error) — NOT "queried and found zero".
    try:
        row = await database.fetch_one(
            """SELECT COALESCE(SUM(api_cost_usd), 0) AS api_usd,
                      COALESCE(SUM(cost), 0) AS credits,
                      COUNT(*) AS calls
               FROM usage_records
               WHERE user_id = ? AND timestamp >= datetime('now', ?)""",
            (user_id, f"-{int(days)} day"),
        )
        total = await database.fetch_one(
            """SELECT COALESCE(SUM(api_cost_usd), 0) AS api_usd,
                      COALESCE(SUM(cost), 0) AS credits,
                      COUNT(*) AS calls
               FROM usage_records
               WHERE user_id = ?""",
            (user_id,),
        )
        return {
            "llm_api_cost_usd": round(float(row.get("api_usd") or 0), 6) if row else 0.0,
            "credits_spent": round(float(row.get("credits") or 0), 4) if row else 0.0,
            "llm_calls": int(row.get("calls") or 0) if row else 0,
            "llm_api_cost_total_usd": round(float(total.get("api_usd") or 0), 6) if total else 0.0,
            "llm_calls_total": int(total.get("calls") or 0) if total else 0,
            "costs_available": True,
        }
    except Exception:
        # Was silent debug — a metering break must be observable, not a false $0
        # to the owner (H14b). Warn, and flag the leg unavailable.
        logger.warning("ledger: usage_records (costs) leg unavailable", exc_info=True)
        return {"llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0,
                "llm_api_cost_total_usd": 0.0, "llm_calls_total": 0,
                "costs_available": False}


def _wallet_leg(user_id: str, days: int) -> Dict[str, Any]:
    # wallet_metering: "on" (event log enabled & read), "disabled" (event log off
    # → wallet spend is silently absent, NOT $0), or "error" (read raised).
    if not user_id:
        # query(user_id=None) means "no filter" — an empty tenant must NEVER
        # widen into the platform-wide spend aggregate (cross-tenant leak)
        return {"wallet_spend_usd": 0.0, "wallet_payments": 0, "wallet_metering": "disabled"}
    try:
        from core.event_log import event_log_enabled, open_event_log
        if not event_log_enabled():
            return {"wallet_spend_usd": 0.0, "wallet_payments": 0,
                    "wallet_metering": "disabled"}
        # A READ never creates the store (2026-09-21): `get_event_log()` ran the
        # DDL and minted an empty telemetry_events.db in whatever home the
        # ledger resolved — which is also how `polyrob doctor` and the console's
        # doctor came to disagree mid-request. No file = no spend recorded yet.
        log = open_event_log()
        if log is None:
            return {"wallet_spend_usd": 0.0, "wallet_payments": 0,
                    "wallet_metering": "on"}
        events = log.query(
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
        return {"wallet_spend_usd": round(total, 6), "wallet_payments": len(events or []),
                "wallet_metering": "on"}
    except Exception:
        logger.warning("ledger: wallet_spend leg unavailable", exc_info=True)
        return {"wallet_spend_usd": 0.0, "wallet_payments": 0, "wallet_metering": "error"}


async def _inbound_leg(database, user_id: str, days: int) -> Dict[str, Any]:
    try:
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
            "inbound_available": True,
        }
    except Exception:
        logger.warning("ledger: x402 inbound leg unavailable", exc_info=True)
        return {"income_usd": 0.0, "settled_payments": 0,
                "pending_invoices_usd": 0.0, "pending_invoices": 0,
                "refund_due_usd": 0.0, "refund_due_count": 0,
                "inbound_available": False}


async def build_ledger(user_id: str, *, days: int = 7, include_balances: bool = False,
                       db=None) -> Dict[str, Any]:
    """The unified balance-sheet view for one tenant over a trailing window.

    Tenant-scoped by contract: an empty user_id is refused (mirrors
    MEMORY_REQUIRE_USER_ID) — anonymity must never widen into an all-tenants view.

    Returns the ``treasury``/``runtime`` blocks (the canonical two-ledger
    contract — see the module docstring) plus a handful of legacy flat keys
    still read by ``ledger_availability_note`` and some renderers
    (``settled_payments``, ``llm_api_cost_usd``, ``wallet_spend_usd``,
    ``credits_spent``, ``llm_calls``, ``pending_invoices*``, ``*_available``,
    ``wallet_metering``). The merged ``total_spend_usd``/top-level ``net_usd``/
    ``earned_usd``/``llm_api_cost_total_usd``/``llm_calls_total`` fields are
    deleted — no deprecation alias, on purpose (Task 8): a surviving key would
    let a consumer keep silently reading the owner's-bill-plus-agent's-outflow
    merge this module exists to kill. ``include_balances`` gates the two
    display-only balance probes (network reads) — both fields stay ``None``
    unless the caller opts in, since this function is also called on hot
    non-display paths (e.g. ``core/recap.py`` via a synchronous bridge) that
    must never block on a network read.

    Also returns a ``caps`` block (A39/A6, 043) — ``daily_cap_usd``/
    ``daily_used_usd``/``daily_left_usd``/``per_tx_cap_usd`` read from the
    wallet ``PolicyGate`` (a local, non-network read, so it runs
    unconditionally). This is configuration/headroom, not money — it is
    never folded into ``treasury``. Every value is ``None`` when the gate
    can't be read (wallet disabled/misconfigured), never a fabricated ``0``.
    """
    if not user_id:
        raise ValueError("accounting requires an authenticated tenant (empty user_id refused)")
    days = max(1, int(days))
    database = await _resolve_db(db)
    # No database at all (standalone caller with no bot.db) => the DB-backed legs
    # are UNAVAILABLE, not zero (H14b): mark them so the renderers show "no data
    # yet" rather than a fabricated $0.00.
    costs = (await _costs_leg(database, user_id, days)) if database is not None else {
        "llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0,
        "llm_api_cost_total_usd": 0.0, "llm_calls_total": 0,
        "costs_available": False}
    inbound = (await _inbound_leg(database, user_id, days)) if database is not None else {
        "income_usd": 0.0, "settled_payments": 0,
        "pending_invoices_usd": 0.0, "pending_invoices": 0,
        "refund_due_usd": 0.0, "refund_due_count": 0,
        "inbound_available": False}
    wallet = _wallet_leg(user_id, days)

    # Pop income_usd (mirrors the two runtime pops below): it belongs ONLY in
    # treasury. Left in `inbound`, it would still be spread at the top level
    # by `**inbound` below — a top-level `income_usd` that looks legitimate
    # (the name survived the Task 8 contract pass) but silently diverges from
    # the error-path fallback shape (`_empty_ledger` in webview/pages.py has
    # no top-level income_usd), exactly the drift this contract exists to kill.
    income_usd = inbound.pop("income_usd")
    treasury = {
        "income_usd": income_usd,
        "spend_usd": wallet["wallet_spend_usd"],
        "pending_usd": inbound["pending_invoices_usd"],
        "pending_count": inbound["pending_invoices"],
        "refund_due_usd": inbound.get("refund_due_usd", 0.0),
        "refund_due_count": inbound.get("refund_due_count", 0),
        "balance_usd": None,
        "net_usd": round(income_usd - wallet["wallet_spend_usd"], 6),
        # H14b: availability must reflect BOTH legs this block depends on, not
        # just the income read. `_wallet_leg` reports "on" / "disabled" /
        # "error" — both "disabled" and "error" mean spend_usd is NOT a
        # trustworthy zero (it's a fabricated $0.00), which makes net_usd
        # untrustworthy too, so the treasury block as a whole is not
        # "available". `ledger_availability_note()` already explains *why* to
        # the user; this flag only says *whether*.
        "available": bool(inbound["inbound_available"]) and wallet["wallet_metering"] == "on",
    }
    runtime = {
        "spend_window_usd": costs["llm_api_cost_usd"],
        "spend_total_usd": costs.pop("llm_api_cost_total_usd"),
        "calls_window": costs["llm_calls"],
        "calls_total": costs.pop("llm_calls_total"),
        "provider_balance_usd": None,
        "available": bool(costs["costs_available"]),
    }
    if include_balances:
        from modules.credits import balances as _bal
        treasury["balance_usd"] = await _bal.treasury_balance_usd(user_id)
        runtime["provider_balance_usd"] = await _bal.provider_balance_usd()
    return {
        "user_id": user_id,
        "window_days": days,
        **costs,
        **wallet,
        **inbound,
        "treasury": treasury,
        "runtime": runtime,
        "caps": _caps_block(),
    }


def ledger_availability_note(ledger: Dict[str, Any]) -> Optional[str]:
    """A one-line honesty note when a ledger leg could not be read (H14b), else
    ``None``. Lets a renderer say "no data yet / metering degraded" instead of
    presenting an absent/broken data layer as an honest-looking $0.00."""
    if not ledger:
        return "no data yet (ledger not readable)"
    costs_ok = bool(ledger.get("costs_available", True))
    inbound_ok = bool(ledger.get("inbound_available", True))
    metering = ledger.get("wallet_metering", "on")
    # Wording deliberately avoids "unavailable" — that word is the legacy
    # container-error line ("finance unavailable (…)") the views must never
    # regress to; tests guard on its absence.
    degraded = []
    if not costs_ok:
        degraded.append("LLM/API cost metering absent (no usage_records table)")
    if not inbound_ok:
        degraded.append("invoice/earnings metering absent (no x402 table)")
    if metering == "disabled":
        degraded.append("wallet-spend metering OFF (TELEMETRY_EVENT_LOG_ENABLED)")
    elif metering == "error":
        degraded.append("wallet-spend metering errored")
    if not costs_ok and not inbound_ok:
        # Both DB-backed money tables are absent => genuinely no data yet.
        return "no data yet — " + "; ".join(degraded)
    if degraded:
        return "metering degraded — " + "; ".join(degraded)
    return None


def format_ledger(ledger: Dict[str, Any]) -> str:
    """Agent/owner-readable rendering of :func:`build_ledger` (pure).

    Two statements, never summed. Treasury is Rob's money and is the only side
    with a net; runtime cost is the owner's API bill and has nothing to net
    against. A None balance is OMITTED (unknown), never rendered as $0.00.
    """
    t, r = ledger["treasury"], ledger["runtime"]
    lines = [f"Ledger — last {ledger['window_days']}d (tenant {ledger['user_id'] or '(all)'})",
             "  Treasury (agent's own money)",
             f"    income:   ${t['income_usd']:.4f}",
             f"    spend:    ${t['spend_usd']:.4f}",
             f"    pending:  ${t['pending_usd']:.4f} across {t['pending_count']} open invoice(s)",
             f"    net:      ${t['net_usd']:+.4f}"]
    if t.get("refund_due_count"):
        lines.append(f"    ⚠ refund owed: ${t['refund_due_usd']:.4f} across "
                     f"{t['refund_due_count']} settled payment(s) we did not deliver on")
    if t["balance_usd"] is not None:
        lines.append(f"    balance:  ${t['balance_usd']:.4f}")
    lines += ["  Runtime cost (owner-funded compute)",
              f"    spend:    ${r['spend_window_usd']:.4f} this window "
              f"({r['calls_window']} calls)",
              f"    total:    ${r['spend_total_usd']:.4f} lifetime "
              f"({r['calls_total']} calls)"]
    if r["provider_balance_usd"] is not None:
        lines.append(f"    balance:  ${r['provider_balance_usd']:.4f}")
    body = "\n".join(lines)
    note = ledger_availability_note(ledger)
    if note:
        body += f"\n  ⚠ {note}"
    return body
