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
- **inbound** — payment receipts (settled rows) plus the open pipeline (pending
  agent invoices), read through the ``credits.inbound_receipts`` hook
  (``register_inbound_receipts``; today the x402 rail registers it).
"""
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class ReadOnlyBotDb:
    """``fetch_one`` / ``fetch_all`` over ``core.sqlite_util.read_only_connect``
    (116 W6.1): the db a ledger read takes when there is no container, or when it
    runs off the main thread. It opens the file ``mode=ro`` per call — it never
    creates bot.db, never takes the WAL write path, and never touches the shared
    connection (whose ``asyncio.Lock`` is bound to the main loop and gives no
    exclusion across threads). The legs keep their SQL."""

    def __init__(self, path: str):
        self.path = str(path)

    def _query(self, query: str, params, one: bool):
        from core.sqlite_util import read_only_connect
        conn = read_only_connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(query, tuple(params or ()))
            if one:
                row = cur.fetchone()
                return dict(row) if row is not None else None
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    async def fetch_one(self, query: str, params=()) -> Optional[Dict[str, Any]]:
        return self._query(query, params, True)

    async def fetch_all(self, query: str, params=()):
        return self._query(query, params, False)


def _container_db_path(container) -> Optional[str]:
    """The path the container's own bot.db connection opened, else its config's
    ``data_dir`` through the one rule. None when neither can be read."""
    try:
        dbm = container.get_service("database_manager")
        path = getattr(getattr(dbm, "connection", None), "db_path", None)
        if path:
            return str(path)
    except Exception:
        logger.debug("ledger: container has no database_manager", exc_info=True)
    cfg = getattr(container, "config", None)
    data_dir = getattr(cfg, "data_dir", None)
    if data_dir:
        from core.runtime_paths import bot_db_path
        return bot_db_path(str(data_dir))
    return None


async def _resolve_db(db=None, data_dir: Optional[str] = None) -> Tuple[Any, Optional[str]]:
    """``(db, reason)``: the db the DB-backed legs read, or ``None`` and the real
    reason it could not be opened (116 W6.1 — never a fixed "no table" text).

    - an explicit ``db`` wins;
    - a container on the MAIN thread: its shared ``database_manager``;
    - otherwise (no container — ``polyrob doctor``, the recap, a bare CLI — or a
      call off the main thread, the snapshot's bridge read): a
      :class:`ReadOnlyBotDb` over the container's path, else
      ``core.runtime_paths.bot_db_path(data_dir)``.

    2026-09-21: ``get_instance()`` RAISES when no container was ever built, so
    the instance is read directly; no container is never a raise here."""
    if db is not None:
        return db, None
    from core.container import DependencyContainer
    container = DependencyContainer._instance
    path = None
    if container is not None:
        if threading.current_thread() is threading.main_thread():
            try:
                shared = container.get_service("database_manager")
            except Exception as e:
                return None, f"no database_manager ({type(e).__name__}: {e})"
            if shared is not None:
                return shared, None
        path = _container_db_path(container)
    if not path:
        from core.runtime_paths import bot_db_path
        path = bot_db_path(data_dir)
    if not os.path.isfile(path):
        return None, f"no bot.db at {path}"
    return ReadOnlyBotDb(path), None


def _policy_gate():
    """Seam over ``core.money.hooks.get_spend_ledger()`` (the wallet registers
    ``core.wallet.factory.get_policy_gate``) — module-level so a
    test can monkeypatch it directly (A39/A6, 043). Fail-open to ``None`` on
    ANY exception (wallet disabled, misconfigured env, no data home yet):
    the caps block this feeds must degrade to all-``None``, never a
    fabricated ``$0.00`` cap."""
    try:
        # 067 P1b: the kernel ledger getter; the wallet registers the provider.
        from core.money.hooks import get_spend_ledger
        return get_spend_ledger()
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


def _operator_scope_ok(user_id: str) -> bool:
    """CR-M07: the operator wallet's balances and PolicyGate headroom are the
    OWNER's facts, not a tenant's. ONE seam (``core.money.authority.
    owner_refusal``); a probe error fails CLOSED (withheld, rendered unknown)."""
    try:
        from core.money.authority import owner_refusal
        return owner_refusal(user_id) is None
    except Exception:
        logger.warning("ledger: owner probe failed; operator facts withheld",
                       exc_info=True)
        return False


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


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def _costs_unavailable(reason: str) -> Dict[str, Any]:
    return {"llm_api_cost_usd": 0.0, "credits_spent": 0.0, "llm_calls": 0,
            "llm_api_cost_total_usd": 0.0, "llm_calls_total": 0,
            "costs_available": False, "costs_reason": reason}


def _inbound_unavailable(reason: str) -> Dict[str, Any]:
    return {"income_usd": 0.0, "settled_payments": 0,
            "pending_invoices_usd": 0.0, "pending_invoices": 0,
            "refund_due_usd": 0.0, "refund_due_count": 0,
            "inbound_available": False, "inbound_reason": reason}


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
    except Exception as e:
        # Was silent debug — a metering break must be observable, not a false $0
        # to the owner (H14b). Warn, and flag the leg unavailable WITH its cause.
        logger.warning("ledger: usage_records (costs) leg unavailable", exc_info=True)
        return _costs_unavailable(_reason(e))


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
        totals = log.aggregate(
            kind="wallet_spend", user_id=user_id,
            since_ts=time.time() - days * 86400,
        )
        return {"wallet_spend_usd": round(totals["wallet_spend_usd"], 6),
                "wallet_payments": totals["counts_by_kind"].get("wallet_spend", 0),
                "wallet_metering": "on"}
    except Exception:
        logger.warning("ledger: wallet_spend leg unavailable", exc_info=True)
        return {"wallet_spend_usd": 0.0, "wallet_payments": 0, "wallet_metering": "error"}


#: 067 P5a — the ``credits.inbound_receipts`` hook: an async
#: ``reader(database, user_id, days) -> {income_usd, settled_payments,
#: pending_invoices_usd, pending_invoices, refund_due_usd, refund_due_count}``
#: that raises on a read failure. The credits module names no payment table;
#: the rail that owns the receipts registers the reader (today
#: ``modules/x402/inbound_receipts.py``, imported on first use through
#: ``_IN_TREE_RECEIPTS``; the wallet pack after 067 P5c). No reader = the
#: inbound leg is UNAVAILABLE (never a fabricated $0.00).
_INBOUND_READER = None
_IN_TREE_RECEIPTS = ("modules.x402.inbound_receipts",)
_receipts_loaded = False


def register_inbound_receipts(reader) -> None:
    """Register the inbound receipts reader (last writer wins)."""
    global _INBOUND_READER
    if not callable(reader):
        raise TypeError(f"inbound receipts reader {reader!r} is not callable")
    _INBOUND_READER = reader


def _inbound_reader():
    global _receipts_loaded
    if _INBOUND_READER is None and not _receipts_loaded:
        _receipts_loaded = True
        import importlib
        for name in _IN_TREE_RECEIPTS:
            try:
                importlib.import_module(name)
            except ImportError:
                logger.debug("ledger: no inbound receipts rail installed (%s)", name)
    return _INBOUND_READER


async def _inbound_leg(database, user_id: str, days: int) -> Dict[str, Any]:
    try:
        reader = _inbound_reader()
        if reader is None:
            raise LookupError("no inbound receipts reader is registered")
        return {**(await reader(database, user_id, days)), "inbound_available": True}
    except Exception as e:
        logger.warning("ledger: x402 inbound leg unavailable", exc_info=True)
        return _inbound_unavailable(_reason(e))


async def build_ledger(user_id: str, *, days: int = 7, include_balances: bool = False,
                       db=None, data_dir: Optional[str] = None) -> Dict[str, Any]:
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
    database, db_reason = await _resolve_db(db, data_dir)
    # No database at all (standalone caller with no bot.db) => the DB-backed legs
    # are UNAVAILABLE, not zero (H14b), and each carries the real reason (W6.1).
    if database is None:
        # An absent bot.db is a fact about a fresh home, and the leg says so; only
        # a store that exists and cannot be read is worth a warning line.
        (logger.debug if str(db_reason or "").startswith("no bot.db")
         else logger.warning)("ledger: bot.db not readable: %s", db_reason)
        costs = _costs_unavailable(db_reason or "bot.db not readable")
        inbound = _inbound_unavailable(db_reason or "bot.db not readable")
    else:
        costs = await _costs_leg(database, user_id, days)
        inbound = await _inbound_leg(database, user_id, days)
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
        # 098 MO-21: recorded as paid with no on-chain proof — never in income/net.
        "unproven_usd": inbound.pop("unproven_usd", 0.0),
        "unproven_count": inbound.pop("unproven_count", 0),
        "unproven_auth_used_count": inbound.pop("unproven_auth_used_count", 0),
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
    # CR-M07: a non-owner tenant never sees the operator wallet's balance, the
    # provider credit, or the spend-cap headroom — they render unknown (None).
    operator_ok = _operator_scope_ok(user_id)
    if include_balances and operator_ok:
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
        "caps": _caps_block() if operator_ok else {
            "daily_cap_usd": None, "daily_used_usd": None,
            "daily_left_usd": None, "per_tx_cap_usd": None},
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
    # 116 W6.1: the leg's own reason, never a fixed "no table" for every cause.
    if not costs_ok:
        degraded.append("LLM/API cost metering absent "
                        f"({ledger.get('costs_reason') or 'usage_records not read'})")
    if not inbound_ok:
        degraded.append("invoice/earnings metering absent "
                        f"({ledger.get('inbound_reason') or 'x402 table not read'})")
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

    Two statements, never summed. Treasury is the agent's money and is the only side
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
    if t.get("unproven_count"):
        used = t.get("unproven_auth_used_count") or 0
        lines.append(f"    unproven: ${t['unproven_usd']:.4f} across {t['unproven_count']} "
                     "payment(s) with no on-chain tx (not income)"
                     + (f"; {used} with the payer's authorization used on chain" if used else ""))
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
