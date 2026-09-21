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


#: What an UNKNOWN amount renders as. A figure that was never read is not zero.
_UNKNOWN = "—"


def _money(value) -> str:
    """Render a USD amount honestly.

    Three distinct answers, never merged (C49):
      * a real amount           -> ``$12.34`` (sub-cent-but-nonzero keeps 4dp so
        a real $0.0003 spend never collapses to a ``$0.00`` lie, L10);
      * a measured zero         -> ``$0.00``;
      * ``None`` / unparseable  -> ``—``. It used to render ``$0.00``, which is
        the confident-zero this whole surface exists to refuse: "I did not read
        this" and "this is zero" are different facts and the owner acts on them
        differently.
    """
    if value is None:
        return _UNKNOWN
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _UNKNOWN
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


# A39/A6 (043): the single wording for "this block's numbers could not be
# read" — used for BOTH the treasury and the runtime block, so the two never
# drift into two different honesty stories for the same failure shape.
_UNAVAILABLE_REASON = "not readable — metering is off or the store is missing"


def _cap_headroom_value(ledger: dict) -> str:
    """The 'today's cap' row value: real headroom when the wallet PolicyGate
    was readable, else an honest dash + reason (A39) — never a fabricated
    number. ``ledger["caps"]`` comes straight from
    ``unified_ledger.build_ledger`` (the same PolicyGate read the webview
    Finance page uses)."""
    caps = ledger.get("caps") or {}
    cap = caps.get("daily_cap_usd")
    used = caps.get("daily_used_usd")
    left = caps.get("daily_left_usd")
    if cap is None or used is None or left is None:
        return "—   cap headroom unavailable (wallet not readable)"
    return f"{_money(used)} of {_money(cap)} ({_money(left)} left)"


def render_finance_text(ledger: dict, *, days: int, user_id: str) -> str:
    """Pure renderer over an ALREADY-BUILT ledger dict (extracted from
    ``render_finance`` in A39/A6, 043, so the honesty-per-block logic is
    testable without the async ``build_ledger`` round trip).

    H14b/A39: a block (``treasury``/``runtime``) whose ``available`` flag is
    ``False`` renders every one of its rows as ``—`` plus one reason line —
    the ledger has ALREADY marked that leg unreadable; re-computing real
    numbers from its (fabricated-zero) fields would re-launder a "we
    couldn't read it" into an honest-looking $0.00 balance sheet. When both
    blocks ARE available, rendering is unchanged from before this
    extraction — same rows, same spacing.
    """
    header = f"finance — last {int(ledger.get('window_days') or days)} days (tenant {user_id})"

    # H14b: an absent DB-backed money layer ("no data yet") must NOT be rendered
    # as an honest-looking $0.00. When both money tables are missing, show the
    # honest empty-state; a partial degrade is annotated below the numbers.
    note = ledger_availability_note(ledger)
    costs_ok = bool(ledger.get("costs_available", True))
    inbound_ok = bool(ledger.get("inbound_available", True))
    if not costs_ok and not inbound_ok:
        # C48: not "no data yet, or metering off, or not initialized" — a
        # disjunction is three guesses printed as one answer. BOTH money stores
        # refused this read, so the honest word is unavailable, with whatever
        # reason the ledger itself carries.
        return "\n".join([
            header,
            "",
            f"{candy.GUTTER}unavailable — I could not read either money store, "
            "so income and runtime cost are UNKNOWN, not zero.",
            f"{candy.GUTTER}({note})" if note else "",
        ]).rstrip()

    # Two statements, never summed: treasury is the agent's own USDC
    # (income/spend/pending/net); runtime is the owner's LLM/API bill (no net —
    # there's nothing to net an expense against). Reading the legacy merged
    # `earned_usd`/`total_spend_usd`/`net_usd` here would report the owner's
    # API bill as part of the agent's own P&L.
    t = ledger.get("treasury") or {}
    r = ledger.get("runtime") or {}
    t_available = t.get("available", True) is not False
    r_available = r.get("available", True) is not False

    if t_available:
        income = float(t.get("income_usd") or 0.0)
        t_spend = float(t.get("spend_usd") or 0.0)
        pending = float(t.get("pending_usd") or 0.0)
        net = float(t.get("net_usd") or 0.0)
        t_bal = t.get("balance_usd")
        treasury_rows = [
            ("income", f"{_money(income)}   ({int(ledger.get('settled_payments') or 0)} settled)"),
            ("spend", _money(t_spend)),
            ("pending", f"{_money(pending)}   ({int(t.get('pending_count') or 0)} open invoices)"),
            ("net", _money(net)),
        ]
        if t_bal is not None:
            treasury_rows.append(("balance", _money(t_bal)))
        treasury_reason = None
    else:
        # A39: the ledger already flagged this leg unreadable — every row a
        # dash, not the fabricated $0.00 the raw fields would otherwise carry.
        treasury_rows = [("income", "—"), ("spend", "—"), ("pending", "—"), ("net", "—")]
        treasury_reason = _UNAVAILABLE_REASON
    # The wallet daily-cap headroom is its OWN read (PolicyGate, not the
    # treasury income/spend legs) — it renders regardless of treasury.available.
    # Fix round 1: rendered through its OWN candy.kv_lines call (its own
    # alignment scope), never appended into treasury_rows — kv_lines pads
    # every label to the longest key in ONE call, so folding "today's cap"
    # (12 chars) into the same call as "income"/"spend"/"pending"/"net"/
    # "balance" (<=7 chars) shifted every pre-existing row's value column by
    # 4 spaces, breaking byte-identical rendering when available is True.
    cap_line = candy.kv_lines([("today's cap", _cap_headroom_value(ledger))])
    # Money we TOOK and did not deliver on. It is never netted into `income`
    # (`INCOME_STATUSES` excludes `refund_due`) and it is a liability, so it
    # leads with the warning rather than sitting in the row block. Rendered
    # only when there IS one — a permanent "$0.00 owed" row trains the eye to
    # skip the line that matters. Its OWN `kv_lines` scope for the same reason
    # `today's cap` has one: folding a 12-char label into `treasury_rows`
    # re-pads every row above it.
    refund_line = ""
    if t_available and int(t.get("refund_due_count") or 0) > 0:
        refund_line = (f"{candy.GUTTER}⚠ refund owed: "
                       f"{_money(t.get('refund_due_usd'))} across "
                       f"{int(t.get('refund_due_count'))} settled payment(s) "
                       f"I did not deliver on — "
                       f"`polyrob owner invoices --status refund_due` lists "
                       f"them (`/invoices refund_due` in chat)")

    if r_available:
        r_window = float(r.get("spend_window_usd") or 0.0)
        r_total = float(r.get("spend_total_usd") or 0.0)
        r_bal = r.get("provider_balance_usd")
        runtime_rows = [
            ("spend", f"{_money(r_window)}   ({int(r.get('calls_window') or 0)} calls)"),
            ("total", f"{_money(r_total)}   ({int(r.get('calls_total') or 0)} calls)"),
        ]
        if r_bal is not None:
            runtime_rows.append(("balance", _money(r_bal)))
        runtime_reason = None
    else:
        runtime_rows = [("spend", "—"), ("total", "—")]
        runtime_reason = _UNAVAILABLE_REASON

    lines = [header, "", f"{candy.GUTTER}Treasury (agent's own money)"]
    if treasury_reason:
        lines.append(f"{candy.GUTTER}{treasury_reason}")
    lines.append(candy.kv_lines(treasury_rows))
    if refund_line:
        lines.append(refund_line)
    lines.append(cap_line)
    lines += ["", f"{candy.GUTTER}Runtime cost (owner-funded compute)"]
    if runtime_reason:
        lines.append(f"{candy.GUTTER}{runtime_reason}")
    lines.append(candy.kv_lines(runtime_rows))
    if note:
        lines.append(f"{candy.GUTTER}⚠ {note}")
    lines += [
        "",
        "(invoices: polyrob owner invoices · settle: polyrob owner settle <id>)",
    ]
    return "\n".join(lines)


def ledger_standalone(user_id: str, *, days: int = 7, db_path: str = None,
                      include_balances: bool = False) -> dict:
    """``build_ledger`` for a process with NO DI container (the CLI seats).

    Opens ``db_path`` (the live ``bot.db``) for the duration of the build and
    closes it; with no path the ledger's legs render unavailable rather than
    raise. Shared by ``/finance``, ``polyrob finance`` and ``polyrob doctor``
    (2026-09-21) so the three cannot read three different stores."""
    from core.async_bridge import run_coroutine_sync

    async def _build():
        if db_path:
            from pathlib import Path
            from modules.database.connection import DatabaseConnection
            db = DatabaseConnection(Path(db_path))
            await db.connect()
            try:
                return await build_ledger(user_id, days=max(1, int(days)), db=db,
                                          include_balances=include_balances)
            finally:
                await db.close()
        return await build_ledger(user_id, days=max(1, int(days)),
                                  include_balances=include_balances)

    return run_coroutine_sync(_build()) or {}


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
        # C48: one fact, not a disjunction, and no flag name — the money store
        # does not exist under this data home, which is exactly what a tree
        # that has never run the agent looks like.
        return "\n".join([
            f"finance — last {int(days)} days (tenant {user_id})",
            "",
            f"{candy.GUTTER}unavailable — there is no money store under this "
            "data home yet, so every figure here is UNKNOWN, not zero.",
            f"{candy.GUTTER}it is created the first time the agent runs: "
            "`polyrob run \"hello\"`.",
        ])
    try:
        # CLI /finance is a DISPLAY surface (mirrors the webview Finance
        # page) -> opt into the two balance probes.
        ledger = ledger_standalone(user_id, days=days, db_path=db_path,
                                   include_balances=True) or {}
    except Exception as e:
        return f"{candy.GUTTER}finance unavailable ({e})"

    return render_finance_text(ledger, days=days, user_id=user_id)


def h_finance(ctx) -> None:
    """REPL handler: /finance [days]  e.g. /finance 30, /finance 7d."""
    days = _days_from_arg(ctx.args[0] if getattr(ctx, "args", None) else "")
    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"
    ctx.emit(render_finance(user_id=uid, days=days), title="finance")
