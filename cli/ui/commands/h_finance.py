"""`/finance` — two statements, never summed: the agent's own Treasury
(income, spend, pending, net) and Runtime cost (the owner's LLM/API bill).

A consumer over ``modules.credits.unified_ledger.build_ledger`` — the SAME core
the webview `/finance` page renders, so the numbers can never disagree across
surfaces (2026-07-12 UI-surface review, gap G3: the ledger used to be
webview-only). This module is only the rendering layer; both the REPL handler
and the ``polyrob finance`` Click command share :func:`render_finance`.

No ``from __future__ import annotations`` (kept consistent with the CLI
command modules; unnecessary here).
"""
from cli.ui import candy
from modules.credits.unified_ledger import build_ledger, ledger_availability_note


def _money(value) -> str:
    """Render a USD amount honestly: sub-cent-but-nonzero shows 4dp so a real
    $0.0003 spend never collapses to a $0.00 lie (L10); everything else 2dp."""
    try:
        v = float(value or 0.0)
    except Exception:
        v = 0.0
    if v != 0 and abs(v) < 0.01:
        return f"${v:.4f}"
    return f"${v:.2f}"


def _days_from_arg(arg: str) -> int:
    """Parse '7' or '7d' -> days; fail-open to 7 (display window, never data-critical)."""
    if not arg:
        return 7
    label = arg.strip().lower().rstrip("d")
    try:
        return max(1, int(label))
    except Exception:
        return 7


def render_finance(*, user_id: str, days: int = 7, db_path: str = None,
                   standalone: bool = False) -> str:
    """Pure renderer: one plain-text balance sheet over ``build_ledger``.

    Fail-open: any ledger error renders an explicit "unavailable" line rather
    than raising — same discipline as ``h_journey``/``core.recap`` seams.

    H14a: ``db_path`` lets a STANDALONE caller (the ``polyrob finance`` command,
    which has no live DI container) pass a resolved bot.db so the ledger legs
    actually run — a real balance sheet, not the container-error "unavailable"
    line. The REPL handler omits it (db=None → build_ledger uses the container).

    ``standalone`` (H14a): the ``polyrob finance`` command sets this. With no
    ``db_path`` AND no container to fall back on, there is genuinely no data yet
    — render an honest "no data yet" sheet instead of leaking the developer-speak
    container error ("Configuration required for first initialization").
    """
    if standalone and not db_path:
        # No bot.db resolved and no DI container: this is a fresh install that has
        # never recorded money activity (H14a). Say so honestly — don't crash into
        # the container's "Configuration required for first initialization" error.
        return "\n".join([
            f"finance — last {int(days)} days (tenant {user_id})",
            "",
            f"{candy.GUTTER}no data yet — the agent hasn't recorded any money "
            "activity, or it has not run yet.",
            f"{candy.GUTTER}(run the agent once; income needs X402_INVOICE_ENABLED)",
        ])
    try:
        from core.async_bridge import run_coroutine_sync

        async def _build():
            # CLI /finance is a DISPLAY surface (mirrors the webview Finance
            # page) -> opt into the two balance probes.
            if db_path:
                from pathlib import Path
                from modules.database.connection import DatabaseConnection
                db = DatabaseConnection(Path(db_path))
                await db.connect()
                try:
                    return await build_ledger(user_id, days=max(1, int(days)), db=db,
                                              include_balances=True)
                finally:
                    await db.close()
            return await build_ledger(user_id, days=max(1, int(days)), include_balances=True)

        ledger = run_coroutine_sync(_build()) or {}
    except Exception as e:
        return f"{candy.GUTTER}finance unavailable ({e})"

    header = f"finance — last {int(ledger.get('window_days') or days)} days (tenant {user_id})"

    # H14b: an absent DB-backed money layer ("no data yet") must NOT be rendered
    # as an honest-looking $0.00. When both money tables are missing, show the
    # honest empty-state; a partial degrade is annotated below the numbers.
    note = ledger_availability_note(ledger)
    costs_ok = bool(ledger.get("costs_available", True))
    inbound_ok = bool(ledger.get("inbound_available", True))
    if not costs_ok and not inbound_ok:
        return "\n".join([
            header,
            "",
            f"{candy.GUTTER}no data yet — the agent hasn't recorded any money "
            "activity, or metering is off / not yet initialized.",
            f"{candy.GUTTER}({note})" if note else "",
        ]).rstrip()

    # Two statements, never summed: treasury is the agent's own USDC
    # (income/spend/pending/net); runtime is the owner's LLM/API bill (no net —
    # there's nothing to net an expense against). Reading the legacy merged
    # `earned_usd`/`total_spend_usd`/`net_usd` here would report the owner's
    # API bill as part of the agent's own P&L.
    t = ledger.get("treasury") or {}
    r = ledger.get("runtime") or {}
    income = float(t.get("income_usd") or 0.0)
    t_spend = float(t.get("spend_usd") or 0.0)
    pending = float(t.get("pending_usd") or 0.0)
    net = float(t.get("net_usd") or 0.0)
    r_window = float(r.get("spend_window_usd") or 0.0)
    r_total = float(r.get("spend_total_usd") or 0.0)
    r_bal = r.get("provider_balance_usd")
    t_bal = t.get("balance_usd")

    treasury_rows = [
        ("income", f"{_money(income)}   ({int(ledger.get('settled_payments') or 0)} settled)"),
        ("spend", _money(t_spend)),
        ("pending", f"{_money(pending)}   ({int(t.get('pending_count') or 0)} open invoices)"),
        ("net", _money(net)),
    ]
    if t_bal is not None:
        treasury_rows.append(("balance", _money(t_bal)))
    runtime_rows = [
        ("spend", f"{_money(r_window)}   ({int(r.get('calls_window') or 0)} calls)"),
        ("total", f"{_money(r_total)}   ({int(r.get('calls_total') or 0)} calls)"),
    ]
    if r_bal is not None:
        runtime_rows.append(("balance", _money(r_bal)))
    lines = [
        header, "",
        f"{candy.GUTTER}Treasury (agent's own money)",
        candy.kv_lines(treasury_rows), "",
        f"{candy.GUTTER}Runtime cost (owner-funded compute)",
        candy.kv_lines(runtime_rows),
    ]
    if note:
        lines.append(f"{candy.GUTTER}⚠ {note}")
    lines += [
        "",
        "(invoices: polyrob owner invoices · settle: polyrob owner settle <id>)",
    ]
    return "\n".join(lines)


def h_finance(ctx) -> None:
    """REPL handler: /finance [days]  e.g. /finance 30, /finance 7d."""
    days = _days_from_arg(ctx.args[0] if getattr(ctx, "args", None) else "")
    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    ctx.emit(render_finance(user_id=uid, days=days), title="finance")
