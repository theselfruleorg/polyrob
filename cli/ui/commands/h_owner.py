"""h_owner.py — the REPL's owner control-plane verbs (proposal 030 WS-C C2, G1).

The local REPL was the ONLY interactive owner seat without the owner pause and
the money/admin verbs: Telegram has /pause /resume /asks /fulfill /allow /deny
/allowlist /invoices /settle (``surfaces/telegram/harness.py``) and the CLI has
``polyrob owner halt/resume/asks/fulfill/allow/deny/allowlist/invoices/settle``
(``cli/commands/owner.py``). These handlers close that gap.

One core, many renderers: every handler here is THIN plumbing over the SAME
primitives the other seats call —
- owner pause (031): ``core.surfaces.owner_admin.pause_autonomy``/``resume_autonomy_scopes``
  (honest VERIFIED-state reporting, never a checkmark on an unproven halt);
- asks: the goal-board asks store (``agents.task.goals.board.GoalBoard.asks``/
  ``fulfill_ask`` at ``goals_db_path(get_data_root())`` — the CLI's
  ``_goal_board`` resolution);
- outbound allowlist: the pure ``_do_allow``/``_do_deny``/``_do_allowlist``
  handlers in ``cli/commands/owner.py`` over ``core.surfaces.outbound_allowlist``;
- invoices/settle: ``modules.x402.invoicing.list_payment_requests``/
  ``settle_payment_request`` — the live container DB when present, else the
  CLI's own bot.db resolution (``cli.commands.owner._with_bot_db``).

No business logic lives here — only arg parsing and output formatting. All
output flows through ``ctx.emit`` (the scrubbed choke point); never
``console.print``. Feature-off honesty: flag-gated features print the same
"feature off + remedy" note grammar as ``cli/_flag_warn.py``, never a stack
trace. Fail-open per the handler contract — a broken store degrades to an
honest one-liner, never a REPL teardown.

No ``from __future__ import annotations`` (kept consistent with h_finance.py).
"""
from typing import Optional

from core.runtime_paths import data_dir_or_home

from cli.ui import candy


# ---------------------------------------------------------------------------
# Shared plumbing (resolution only — the business logic lives in core/modules)
# ---------------------------------------------------------------------------


def _admin_data_dir(ctx) -> str:
    """Same home resolution as /config, /pending: the container config's
    data_dir when present, else the resolved data home (POLYROB_DATA_DIR wins,
    else cwd/.polyrob) — the SAME home the surface daemons and `polyrob owner`
    read."""
    cfg = getattr(ctx.container, "config", None) if getattr(ctx, "container", None) else None
    return data_dir_or_home(getattr(cfg, "data_dir", None))


def _tenant(ctx) -> str:
    """The REPL session's tenant — the SAME user_id the runtime writes rows
    under (the live session's identity; "local" when unbound), matching the
    CLI's ``_allowlist_tenant``/``_money_tenant`` resolution."""
    return (getattr(ctx, "user_id", "") or "").strip() or "local"


def _goal_board():
    """The SAME board resolution as ``polyrob owner``'s ``_goal_board`` and the
    REPL's /goals handler."""
    from agents.task.goals.board import GoalBoard
    from core.runtime_config import get_data_root
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(get_data_root()))


def _run_owner_db(ctx, coro_factory):
    """``(ok, result_or_msg)`` over the right money DB.

    Prefer the live container's ``database_manager`` (the same rows the running
    agent writes — what telegram's owner verbs read via
    ``modules.x402._db.resolve_db``); with no live container, fall back to the
    CLI's own bot.db resolution (``cli.commands.owner._with_bot_db`` — DB_PATH
    env wins, else the data-home candidates), which reports "no bot.db found"
    honestly instead of a container bootstrap error.
    """
    from core.async_bridge import run_coroutine_sync

    async def _run():
        db = None
        try:
            if getattr(ctx, "container", None) is not None:
                db = ctx.container.get_service("database_manager")
        except Exception:
            db = None
        if db is not None:
            return True, await coro_factory(db)
        from cli.commands.owner import _with_bot_db
        return await _with_bot_db(coro_factory)

    return run_coroutine_sync(_run())


def _invoicing_off_note() -> str:
    """The ONE feature-off note (``modules.x402.invoicing_note``), returned as
    text so it flows through ``ctx.emit`` (the scrubbed REPL output choke
    point) instead of raw stderr."""
    from modules.x402.invoicing_note import invoicing_off_note
    note = invoicing_off_note(remedy=True)
    return f"note: {note}" if note else ""


# ---------------------------------------------------------------------------
# /pause, /halt (alias) and /resume — the owner pause record (031)
# ---------------------------------------------------------------------------


def _pause_common(ctx, scopes_args, *, force_all: bool = False) -> None:
    """Shared body of /pause and /halt: thin over the ONE owner-admin pause API
    (the same primitive `polyrob autonomy pause` and telegram /pause call); the
    reply is the VERIFIED (read-back) state, never a checkmark on a write."""
    from core.surfaces.owner_admin import pause_autonomy, render_pause_result
    from core.surfaces.owner_intent import parse_pause_args
    try:
        scopes, minutes = parse_pause_args([] if force_all else list(scopes_args or []))
    except ValueError as e:
        ctx.emit(f"{candy.GUTTER}({e})", title="pause")
        return
    try:
        res = pause_autonomy(_admin_data_dir(ctx), scopes=scopes, duration_minutes=minutes,
                             reason="REPL /halt" if force_all else "REPL /pause", via="repl")
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(pause failed: {e})", title="pause")
        return
    ctx.emit(render_pause_result(res, resume_hint="/resume"), title="pause")


def h_pause(ctx) -> None:
    """Pause work now: `/pause [word…] [for 6h]` (default: everything)."""
    _pause_common(ctx, getattr(ctx, "args", []))


def h_halt(ctx) -> None:
    """Alias of `/pause` with no scopes (everything)."""
    _pause_common(ctx, [], force_all=True)


def h_resume(ctx) -> None:
    """Lift the pause: `/resume [word…]` (everything by default).

    Honest verified-state reporting: never claims RESUMED while the runtime
    still reports a pause (e.g. AUTONOMY_HALT set in the env, which only an
    env-file edit + restart can clear).
    """
    from core.surfaces.owner_admin import render_resume_result, resume_autonomy_scopes
    from core.surfaces.owner_intent import parse_pause_args
    try:
        scopes, minutes = parse_pause_args(list(getattr(ctx, "args", []) or []))
    except ValueError as e:
        ctx.emit(f"{candy.GUTTER}({e})", title="resume")
        return
    if minutes is not None:
        ctx.emit(f"{candy.GUTTER}(/resume takes scopes only — e.g. /resume trading)",
                 title="resume")
        return
    try:
        res = resume_autonomy_scopes(_admin_data_dir(ctx),
                                     scopes=None if scopes == ("all",) else scopes, via="repl")
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(resume failed: {e})", title="resume")
        return
    ctx.emit(render_resume_result(res, halt_hint="/pause"), title="resume")


# ---------------------------------------------------------------------------
# /asks and /fulfill — the goal-board asks store
# ---------------------------------------------------------------------------


def h_asks(ctx) -> None:
    """List the agent's OPEN asks — concrete needs blocking its progress.

    Tool-approval asks have their OWN surface (/pending) and are excluded here,
    exactly as in `polyrob owner asks` and telegram /asks.
    """
    if ctx.args and ctx.args[0].lower() != "list":
        ctx.emit("usage: /asks [list]", title="asks")
        return
    tenant = _tenant(ctx)
    try:
        from agents.task.goals.board import ASK_OPEN
        rows = [a for a in _goal_board().asks(user_id=tenant, status=ASK_OPEN)
                if (a.payload or {}).get("ask_kind") != "tool_approval"]
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(asks unavailable: {e})", title="asks")
        return
    if not rows:
        ctx.emit(candy.empty("open asks", "nothing is blocked on you", yet=False),
                 title="asks")
        return
    lines = [f"{len(rows)} open ask(s) for tenant {tenant}:"]
    for a in rows:
        blocks = (a.payload or {}).get("blocks_goal_ids", [])
        lines.append(f"{candy.GUTTER}{a.id}  {a.title}"
                     + (f"  (blocks {len(blocks)} goal(s))" if blocks else ""))
        if a.body:
            lines.append(f"{candy.GUTTER}   {a.body[:200]}")
    lines.append(f"{candy.GUTTER}fulfill one: /fulfill <id>")
    ctx.emit("\n".join(lines), title="asks")


def h_fulfill(ctx) -> None:
    """Mark an ask FULFILLED and flip its blocked goals back to ready."""
    if not ctx.args:
        ctx.emit("usage: /fulfill <ask-id>  (see /asks)", title="fulfill")
        return
    tenant = _tenant(ctx)
    ask_id = ctx.args[0]
    try:
        ok, unblocked = _goal_board().fulfill_ask(ask_id, user_id=tenant)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(fulfill unavailable: {e})", title="fulfill")
        return
    if not ok:
        ctx.emit(f"no open ask '{ask_id}' for tenant {tenant} — see /asks",
                 title="fulfill")
        return
    ctx.emit(f"✅ ask {ask_id} fulfilled — {unblocked} goal(s) unblocked",
             title="fulfill")


# ---------------------------------------------------------------------------
# /missed — owner notices the delivery rail could not send live (A7 / A40)
# ---------------------------------------------------------------------------


def h_missed(ctx) -> None:
    """`/missed [n]` — owner notices the delivery rail could not send live:
    suppressed by the daily cap, held by an owner pause, or undelivered (no
    live sink / send failed). The SAME durable rows Telegram `/missed` and
    `polyrob owner missed` read."""
    args = getattr(ctx, "args", []) or []
    try:
        n = max(1, min(20, int(args[0]))) if args else 5
    except (TypeError, ValueError):
        ctx.emit("usage: /missed [n]  (1-20, default 5)", title="missed")
        return
    tenant = _tenant(ctx)
    try:
        from core.surfaces.missed import format_notice_lines, missed_notices
        rows = missed_notices(tenant, _admin_data_dir(ctx), n)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(missed unavailable: {e})", title="missed")
        return
    if not rows:
        ctx.emit(candy.empty("missed messages", "nothing suppressed", yet=False),
                 title="missed")
        return
    lines = [f"Last {len(rows)} missed owner message(s) (newest first):"]
    for r in rows:
        kind = r.get("kind") or "capped"
        text = str(r.get("text") or "")
        # Wrapped, never clipped — /missed exists to recover text the owner
        # never received live (fix round 1, 2026-09-14).
        lines.extend(format_notice_lines(r.get("ts") or 0, kind, text,
                                         gutter=candy.GUTTER))
    lines.append(f"{candy.GUTTER}raise the cap: /config set delivery.daily_cap N")
    ctx.emit("\n".join(lines), title="missed")


# ---------------------------------------------------------------------------
# /allow, /deny, /allowlist — the outbound-send allowlist
# ---------------------------------------------------------------------------


def _outbound_allowlist(ctx):
    from cli.commands.owner import _allowlist
    return _allowlist(_admin_data_dir(ctx))


def h_allow(ctx) -> None:
    """Allow the agent to send outbound messages to SURFACE:TARGET."""
    if len(ctx.args) < 2:
        ctx.emit("usage: /allow <surface> <target>", title="allow")
        return
    surface, target = ctx.args[0], ctx.args[1]
    tenant = _tenant(ctx)
    try:
        from cli.commands.owner import _do_allow
        _do_allow(_outbound_allowlist(ctx), tenant, surface, target)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(allow failed: {e})", title="allow")
        return
    ctx.emit(f"✅ allowed {surface}:{target} for tenant {tenant}", title="allow")


def h_deny(ctx) -> None:
    """Revoke outbound permission for SURFACE:TARGET."""
    if len(ctx.args) < 2:
        ctx.emit("usage: /deny <surface> <target>", title="deny")
        return
    surface, target = ctx.args[0], ctx.args[1]
    tenant = _tenant(ctx)
    try:
        from cli.commands.owner import _do_deny
        ok = _do_deny(_outbound_allowlist(ctx), tenant, surface, target)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(deny failed: {e})", title="deny")
        return
    if not ok:
        ctx.emit(f"no active allowlist entry {surface}:{target} for tenant {tenant}",
                 title="deny")
        return
    ctx.emit(f"✅ denied {surface}:{target} for tenant {tenant}", title="deny")


def h_allowlist(ctx) -> None:
    """List the outbound-send allowlist for this tenant."""
    tenant = _tenant(ctx)
    try:
        from cli.commands.owner import _do_allowlist
        rows = _do_allowlist(_outbound_allowlist(ctx), tenant)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(allowlist unavailable: {e})", title="allowlist")
        return
    if not rows:
        ctx.emit(candy.empty("allowlist entries", "/allow <surface> <target> adds one",
                             yet=False), title="allowlist")
        return
    lines = [f"{len(rows)} allowlist entr{'y' if len(rows) == 1 else 'ies'} "
             f"for tenant {tenant}:"]
    for r in rows:
        note = f"  ({r['note']})" if r.get("note") else ""
        lines.append(f"{candy.GUTTER}{str(r['status']).ljust(8)} "
                     f"{r['surface']}:{r['target']}{note}")
    ctx.emit("\n".join(lines), title="allowlist")


# ---------------------------------------------------------------------------
# /invoices and /settle — the agent's receivables (x402 invoicing)
# ---------------------------------------------------------------------------

_INVOICE_STATUSES = ("pending", "completed", "expired")


def h_invoices(ctx) -> None:
    """List agent-created x402 payment requests (invoices), tenant-scoped."""
    status: Optional[str] = None
    if ctx.args:
        candidate = ctx.args[0].lower()
        if candidate not in _INVOICE_STATUSES:
            ctx.emit("usage: /invoices [pending|completed|expired]", title="invoices")
            return
        status = candidate
    tenant = _tenant(ctx)

    async def _list(db):
        from modules.x402.invoicing import list_payment_requests
        return await list_payment_requests(user_id=tenant, status=status,
                                           limit=50, db=db)

    try:
        ok, rows = _run_owner_db(ctx, _list)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(invoices unavailable: {e})", title="invoices")
        return
    if not ok:
        ctx.emit(str(rows), title="invoices")
        return
    scope = f"tenant {tenant}" + (f" · {status}" if status else "")
    lines = []
    if not rows:
        lines.append(candy.empty(f"invoices ({scope})", yet=False))
    else:
        lines.append(f"{len(rows)} invoice(s) — {scope}:")
        for r in rows[:20]:
            line = (f"{candy.GUTTER}{str(r['request_id'])[:8]} "
                    f"[{r['status']}] ${float(r.get('amount_usd') or 0):.2f} — "
                    f"{r.get('purpose') or '(no purpose)'}")
            if r.get("payer_contact"):
                line += f" · billed to {r['payer_contact']}"
            lines.append(line)
        if len(rows) > 20:
            lines.append(f"{candy.GUTTER}… (+{len(rows) - 20} more)")
        lines.append(f"{candy.GUTTER}mark one paid: /settle <id> [tx-hash]")
    note = _invoicing_off_note()
    if note:
        lines.append(note)
    ctx.emit("\n".join(lines), title="invoices")


def h_settle(ctx) -> None:
    """Attest an invoice as PAID (pending -> completed) — owner attestation,
    not a payment. Mirrors `polyrob owner settle <id> [--tx-hash]`."""
    if not ctx.args:
        ctx.emit("usage: /settle <invoice-id> [tx-hash]  (see /invoices)",
                 title="settle")
        return
    request_id = ctx.args[0]
    tx_hash = ctx.args[1] if len(ctx.args) > 1 else None

    async def _settle(db):
        from modules.x402.invoicing import settle_payment_request
        return await settle_payment_request(request_id, transaction_hash=tx_hash,
                                            db=db)

    try:
        ok, settled = _run_owner_db(ctx, _settle)
    except Exception as e:
        ctx.emit(f"{candy.GUTTER}(settle unavailable: {e})", title="settle")
        return
    if not ok:
        ctx.emit(str(settled), title="settle")
        return
    if not settled:
        ctx.emit(f"{request_id} not settled (unknown id or not pending) — "
                 "see /invoices pending", title="settle")
        return
    lines = [f"✅ settled {request_id}"]
    note = _invoicing_off_note()
    lines.append(note if note else
                 f"{candy.GUTTER}the settlement watcher will wake the "
                 "originating session (if a polyrob process is running).")
    ctx.emit("\n".join(lines), title="settle")
