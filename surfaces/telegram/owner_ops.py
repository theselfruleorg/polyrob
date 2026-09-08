"""Owner write verbs for chat: cron, goals, wallet, invoices, settle (G13).

Before this, creating a cron job, steering a goal, reading the wallet, listing
invoices or attesting a payment existed on exactly ONE seat — the `polyrob` CLI.
A phone-only owner could watch all of it and change none of it.

These are thin plumbing over the SAME primitives the CLI calls (``CronService``,
``GoalBoard``, ``get_agent_wallet``, ``modules.x402.invoicing``) — never an
import of ``cli/``, and never a second implementation of the rule. They live
here rather than in ``harness.py`` because that file is already god-file sized,
and in ``surfaces/`` rather than ``core/`` because the surfaces tier may import
agents/tools/modules freely (a ``core/`` home would need a new row in the
shrink-only layering ratchet).

Two rules every handler follows:

- **Phone-sized replies.** Lists cap at :data:`_LIST_LIMIT` with a count line;
  every id and path rides in backticks so the renderer emits ``<code>`` and the
  client stops linkifying it.
- **Honest about what will actually happen.** A stored cron job with the ticker
  off, an invoice settled with no watcher running, a cap that needs a restart —
  each says so rather than showing a green checkmark on a no-op.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Rows one chat listing renders before collapsing the rest into a count.
_LIST_LIMIT = 10

#: Separator for `/cron add <schedule> | <task>`. A schedule spec contains
#: spaces ("every monday 09:00") and so does a task, so positional splitting
#: cannot tell them apart.
_CRON_SEP = "|"


def _code(text: Any) -> str:
    return f"`{text}`"


def _tail(shown: int, total: int, what: str) -> Optional[str]:
    if total <= shown:
        return None
    return f"(+{total - shown} more {what} — see the console or ask me)"


# ---------------------------------------------------------------------------
# id prefixes
# ---------------------------------------------------------------------------

def resolve_prefix(prefix: str, ids: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a short id PREFIX against IDS -> ``(full_id, error)``.

    Every chat listing shows truncated ids (`a1b2c3d4`), so requiring the full
    uuid back would make the listings unusable. An ambiguous prefix is an error,
    never a guess — these verbs cancel goals and settle invoices.
    """
    p = (prefix or "").strip()
    if not p:
        return None, "no id given"
    if p in ids:
        return p, None
    matches = [i for i in ids if i.startswith(p)]
    if not matches:
        return None, f"no match for {_code(p)}"
    if len(matches) > 1:
        return None, (f"{_code(p)} matches {len(matches)} ids — "
                      f"use more characters")
    return matches[0], None


# ---------------------------------------------------------------------------
# /cron
# ---------------------------------------------------------------------------

def _cron_service(data_dir: str):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from core.runtime_paths import cron_db_path
    return CronService(CronJobStore(cron_db_path(data_dir)))


def _cron_off_note() -> str:
    """A stored job only runs if the ticker is on — never mislead the owner."""
    try:
        from tools.cronjob_tools import cron_enabled
        if cron_enabled():
            return ""
    except Exception:
        logger.debug("cron enablement probe failed", exc_info=True)
        return ""
    return ("\n⚠️ CRON_ENABLED is off — the job is stored but no ticker will run it.")


def _fmt_job(job) -> str:
    nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
    shot = "once" if job.one_shot else "recurring"
    return (f"• {_code(job.id[:8])} [{job.status}] {job.task}\n"
            f"   {job.schedule_spec} · {shot} · next {nxt}")


def cron_reply(user_id: str, data_dir: str, args: List[str]) -> str:
    """`/cron` — list / show / add / cancel durable scheduled runs."""
    svc = _cron_service(data_dir)
    sub = (args[0].lower() if args else "list")
    rest = args[1:]

    if sub in ("list", "ls"):
        jobs = svc.list_jobs(user_id=user_id)
        if not jobs:
            return ("No cron jobs.\n"
                    f"Add one: /cron add <schedule> {_CRON_SEP} <task>\n"
                    "e.g. /cron add every monday 09:00 | summarise the week")
        shown = jobs[:_LIST_LIMIT]
        lines = [f"{len(jobs)} cron job(s):"] + [_fmt_job(j) for j in shown]
        more = _tail(len(shown), len(jobs), "job(s)")
        if more:
            lines.append(more)
        return "\n".join(lines)

    if sub == "show":
        if not rest:
            return "Usage: /cron show <id>"
        jobs = svc.list_jobs(user_id=user_id)
        job_id, err = resolve_prefix(rest[0], [j.id for j in jobs])
        if err:
            return f"{err} — see /cron."
        job = next(j for j in jobs if j.id == job_id)
        out = [_fmt_job(job),
               f"   created {job.created_at} · last run {job.last_run_at or '-'}",
               f"   max duration {job.max_duration_seconds}s"]
        if job.payload:
            out.append(f"   payload: {_code(job.payload)}")
        return "\n".join(out)

    if sub in ("add", "schedule", "new"):
        raw = " ".join(rest)
        if _CRON_SEP not in raw:
            return (f"Usage: /cron add <schedule> {_CRON_SEP} <task>\n"
                    "Schedules: a duration (30m), 'every monday 09:00', a 5-field "
                    "cron line, or an ISO timestamp.\n"
                    "e.g. /cron add every monday 09:00 | summarise the week")
        spec, _, task = raw.partition(_CRON_SEP)
        spec, task = spec.strip(), task.strip()
        if not spec or not task:
            return f"Both parts are required: /cron add <schedule> {_CRON_SEP} <task>"
        from cron.schedule import ScheduleError
        try:
            job = svc.schedule(task=task, schedule_spec=spec, user_id=user_id)
        except ScheduleError as e:
            return f"Invalid schedule {_code(spec)}: {e}"
        nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
        return (f"✅ Scheduled {_code(job.id[:8])} — next run {nxt}."
                + _cron_off_note())

    if sub in ("cancel", "rm", "delete"):
        if not rest:
            return "Usage: /cron cancel <id>"
        jobs = svc.list_jobs(user_id=user_id)
        job_id, err = resolve_prefix(rest[0], [j.id for j in jobs])
        if err:
            return f"{err} — see /cron."
        if svc.cancel(job_id, user_id=user_id):
            return f"✅ Cancelled {_code(job_id[:8])}."
        return f"Could not cancel {_code(job_id[:8])} — see /cron."

    return ("Usage: /cron [list] · /cron show <id> · "
            f"/cron add <schedule> {_CRON_SEP} <task> · /cron cancel <id>")


# ---------------------------------------------------------------------------
# /goal
# ---------------------------------------------------------------------------

#: verb -> (target status, reset failures, allowed source statuses or None,
#: past participle for the refusal message — `f"{verb}ed"` produces "retryed")
_GOAL_TRANSITIONS = {
    "ready": ("ready", False, ("triage", "blocked"), "marked ready"),
    "pause": ("blocked", False, None, "paused"),
    "resume": ("ready", False, ("blocked",), "resumed"),
    "retry": ("ready", True, ("blocked",), "retried"),
    "cancel": ("cancelled", False, None, "cancelled"),
}


_OBJECTIVE_STATUS_VERBS = {"pause": "paused", "activate": "active", "drop": "dropped"}


def _objective_reply(user_id: str, board: Any, rest: List[str]) -> str:
    """`/goal objective <list|pause|activate|drop> [id]` — steer a whole stream.

    Objectives were CLI-only: a phone-only owner could steer one goal but could
    not see, pause or resume the standing stream behind it. Read plus the three
    lifecycle transitions is the whole surface; CREATE stays on the CLI, because a
    stream needs a body, success criteria and a toolset that chat cannot express
    well (and, for a money stream, a manifest entry rather than a board row).
    """
    from agents.task.goals.board import OBJ_ACTIVE
    sub = (rest[0].lower() if rest else "list")
    if sub == "list":
        objs = board.objectives(user_id=user_id)
        if not objs:
            return ("No objectives. Create one on the CLI: "
                    "polyrob goals objective add \"<title>\"")
        lines = [f"{len(objs)} objective(s):"]
        for o in objs:
            try:
                live = len(board.children_of(user_id, o.id))
            except Exception:
                live = 0
            lines.append(f"• {_code(o.id[:8])} [{o.status}] {o.title} — {live} live")
        lines.append("Steer with /goal objective <pause|activate|drop> <id>.")
        return "\n".join(lines)

    if sub not in _OBJECTIVE_STATUS_VERBS:
        return ("Usage: /goal objective <list|pause|activate|drop> [id]\n"
                "Create an objective on the CLI: polyrob goals objective add \"<title>\"")
    if len(rest) < 2:
        return f"Usage: /goal objective {sub} <id> (see /goal objective list)"

    mine = board.objectives(user_id=user_id)
    oid, err = resolve_prefix(rest[1], [o.id for o in mine])
    if err:
        return f"{err} — see /goal objective list."
    target = _OBJECTIVE_STATUS_VERBS[sub]
    if not board.set_objective_status(oid, target, user_id=user_id):
        return f"Could not update {_code(oid[:8])}."
    note = ("\nIts goals keep running; the planner stops adding new ones."
            if target != OBJ_ACTIVE else "")
    return f"✅ {_code(oid[:8])} → {target}.{note}"


def goal_reply(user_id: str, data_dir: str, args: List[str],
               board: Optional[Any] = None) -> str:
    """`/goal <verb> <id>` — the WRITE counterpart to the read-only `/goals`.

    ``board`` lets the caller share an already-open GoalBoard (the harness opens
    one per request) instead of opening a second connection to the same file.
    """
    from agents.task.goals.board import KIND_GOAL, GoalBoard
    if board is None:
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))

    verb = (args[0].lower() if args else "")
    rest = args[1:]
    if verb == "objective":
        return _objective_reply(user_id, board, rest)
    if verb not in _GOAL_TRANSITIONS and verb != "show":
        return ("Usage: /goal <show|ready|pause|resume|retry|cancel> <id>\n"
                "See /goals for the board.")
    if not rest:
        return f"Usage: /goal {verb} <id> (see /goals)"

    mine = [g for g in board.list(user_id=user_id, limit=1000) if g.kind == KIND_GOAL]
    goal_id, err = resolve_prefix(rest[0], [g.id for g in mine])
    if err:
        return f"{err} — see /goals."
    goal = next(g for g in mine if g.id == goal_id)

    if verb == "show":
        out = [f"{_code(goal.id[:8])} [{goal.status}] {goal.title}"]
        if goal.body:
            out.append(goal.body[:400])
        out.append(f"priority {goal.priority} · failures {goal.consecutive_failures}")
        if goal.last_failure_error:
            out.append(f"last failure: {goal.last_failure_error[:200]}")
        return "\n".join(out)

    target, reset, allowed, participle = _GOAL_TRANSITIONS[verb]
    if allowed is not None and goal.status not in allowed:
        return (f"{_code(goal.id[:8])} is {goal.status} — only "
                f"{'/'.join(allowed)} goals can be {participle}.")
    if goal.status == target:
        return f"{_code(goal.id[:8])} is already {target}."
    warning = ""
    if verb == "pause" and goal.status == "running":
        warning = "\n⚠️ It is running right now — the pause takes effect after this run."
    ok = board.update_status(goal_id, target, reset_failures=reset, user_id=user_id)
    if not ok:
        return f"Could not update {_code(goal.id[:8])} — see /goals."
    suffix = " (failures cleared)" if reset else ""
    return f"✅ {_code(goal.id[:8])} → {target}{suffix}.{warning}"


# ---------------------------------------------------------------------------
# /wallet  (read-only by design)
# ---------------------------------------------------------------------------

#: Why the cap-raising path is deliberately absent from chat.
_CAP_NOTE = (
    "Caps are env-authoritative and a raise needs the env file the running "
    "process reads (a systemd deploy reads its own, e.g. "
    "`/etc/polyrob/polyrob.env`) plus a restart — which chat must never write.\n"
    "To TIGHTEN one from here: /config set budget.wallet_daily_usd <usd> "
    "(guarded → /pending → /approve, applies live, can only lower)."
)


def wallet_reply(args: List[str]) -> str:
    """`/wallet` — addresses, network, caps. Read-only on purpose.

    On-chain balance probes are network reads, so they are opt-in via
    `/wallet balances` rather than paid for on every status glance.
    """
    want_balances = bool(args) and args[0].lower() in ("balances", "balance", "full")
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
    except ValueError as e:
        return f"Wallet unavailable: {e}"
    except Exception as e:
        logger.debug("wallet read failed", exc_info=True)
        return f"Wallet unavailable: {e}"
    if w is None:
        return "Agent wallet is not enabled (AGENT_WALLET_ENABLED)."

    cfg = w.config
    try:
        address = w.address
    except ValueError as e:
        return f"Wallet MISCONFIGURED: {e}"
    lines = [f"Wallet · network {cfg.network} · venue {w.operational_venue}",
             f"Fund: {_code(address)}"]
    # The Solana identity off the same seed (2026-08-27): the owner must see —
    # and be able to fund — this address from the same glance, or it stays
    # invisible until a Solana trade fails on an empty fee balance.
    try:
        sol_addr = w.solana_address
    except Exception:
        sol_addr = None
    if sol_addr:
        lines.append(f"Solana: {_code(sol_addr)}")
    if want_balances:
        if cfg.network != "mainnet":
            lines.append("(balances are mainnet-only — network is "
                         f"{cfg.network})")
        else:
            lines.extend(_wallet_balance_lines(w))
    daily = (f"${cfg.daily_cap_usd:.2f}" if cfg.daily_cap_usd is not None
             else "UNLIMITED")
    lines.append(f"Caps: ${cfg.max_per_tx_usd:.2f}/tx · daily {daily}")
    if cfg.daily_cap_usd is None:
        lines.append("⚠️ The per-tx cap is a catastrophic-loss ceiling, not a "
                     "budget, and the daily cap is unlimited.")
    if not want_balances:
        lines.append("/wallet balances reads the on-chain amounts.")
    lines.append(_CAP_NOTE)
    return "\n".join(lines)


#: Venues that hold a same-chain float at their DERIVED address. hyperliquid
#: (delegated signer) and polymarket (per-user proxy) never do, and showing a
#: balance there re-creates the fund-the-wrong-address footgun. Mirrors
#: ``cli/commands/wallet.py::_FUNDABLE`` — the CLI keeps its own copy; both read
#: the chain map and the balance reader from ``core.wallet.onchain``.
_FUNDABLE_VENUES = ("treasury", "x402")


def _wallet_balance_lines(w) -> List[str]:
    """Per-venue on-chain balances, fail-open to a note (never a traceback)."""
    from core.wallet.onchain import VENUE_CHAIN, balances as _balances
    out = []
    for venue in _FUNDABLE_VENUES:
        try:
            addr = w.signer_for(venue).address
            native, usdc = _balances(addr, VENUE_CHAIN[venue])
        except Exception:
            logger.debug("balance probe failed for %s", venue, exc_info=True)
            out.append(f"  {venue}: balance unavailable")
            continue
        u = f"{usdc:.2f}" if usdc is not None else "n/a"
        n = f"{native:.5f}" if native is not None else "n/a"
        out.append(f"  {venue}: USDC={u} gas={n}")
    try:
        sol_addr = w.solana_address
    except Exception:
        sol_addr = None
    if sol_addr:
        try:
            from core.wallet import chains as _chains
            from core.wallet import solana_onchain
            sol_bal = solana_onchain.native_balance(sol_addr)
            usdc_raw = None
            row = _chains.get("solana")
            if row and row.usdc:
                tb = solana_onchain.token_balances(sol_addr)
                usdc_raw = None if tb is None else tb.get(row.usdc, 0)
            u = f"{usdc_raw / 1e6:.2f}" if usdc_raw is not None else "n/a"
            s = f"{sol_bal:.5f}" if sol_bal is not None else "n/a"
            out.append(f"  solana: USDC={u} SOL={s}")
        except Exception:
            logger.debug("solana balance probe failed", exc_info=True)
            out.append("  solana: balance unavailable")
    return out


# ---------------------------------------------------------------------------
# /invoices and /settle
# ---------------------------------------------------------------------------

def _invoicing_off_note() -> str:
    try:
        from modules.x402.invoicing import x402_invoicing_enabled
        if x402_invoicing_enabled():
            return ""
    except Exception:
        logger.debug("invoicing enablement probe failed", exc_info=True)
        return ""
    return ("\n⚠️ X402_INVOICE_ENABLED is off — rows are durable, but no "
            "settlement watcher runs, so nothing settles on its own.")


async def invoices_reply(user_id: str, args: List[str]) -> str:
    """`/invoices [pending|completed|expired]` — the agent's receivables."""
    status = None
    if args:
        candidate = args[0].lower()
        if candidate in ("pending", "completed", "expired"):
            status = candidate
        else:
            return ("Usage: /invoices [pending|completed|expired]")
    from modules.x402.invoicing import list_payment_requests
    try:
        rows = await list_payment_requests(user_id=user_id, status=status, limit=50)
    except Exception as e:
        logger.warning("telegram /invoices read failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    scope = f"tenant {_code(user_id)}" + (f" · {status}" if status else "")
    if not rows:
        return f"No invoices ({scope})."
    shown = rows[:_LIST_LIMIT]
    lines = [f"{len(rows)} invoice(s) — {scope}:"]
    for r in shown:
        line = (f"• {_code(str(r['request_id'])[:8])} [{r['status']}] "
                f"${float(r.get('amount_usd') or 0):.2f} — "
                f"{r.get('purpose') or '(no purpose)'}")
        if r.get("payer_contact"):
            line += f" · billed to {r['payer_contact']}"
        lines.append(line)
    more = _tail(len(shown), len(rows), "invoice(s)")
    if more:
        lines.append(more)
    lines.append("Mark one paid: /settle <id> [tx-hash]")
    note = _invoicing_off_note()
    if note:
        lines.append(note.strip())
    return "\n".join(lines)


async def settle_reply(user_id: str, args: List[str]) -> str:
    """`/settle <id> [tx-hash]` — attest an invoice as PAID (pending → completed).

    Owner attestation, not a payment: it records that money arrived. Scoped to
    the caller's own tenant, so a chat id can never settle another tenant's row.
    """
    if not args:
        return "Usage: /settle <invoice-id> [tx-hash] (see /invoices)"
    from modules.x402.invoicing import list_payment_requests, settle_payment_request
    try:
        rows = await list_payment_requests(user_id=user_id, status="pending", limit=200)
    except Exception as e:
        logger.warning("telegram /settle lookup failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    request_id, err = resolve_prefix(args[0], [str(r["request_id"]) for r in rows])
    if err:
        return f"{err} among your PENDING invoices — see /invoices pending."
    tx_hash = args[1] if len(args) > 1 else None
    try:
        settled = await settle_payment_request(request_id, transaction_hash=tx_hash)
    except Exception as e:
        logger.warning("telegram /settle failed: %s", e, exc_info=True)
        return f"Settle failed: {e}"
    if not settled:
        return (f"{_code(request_id[:8])} was not settled — it may already be "
                "completed, or that tx hash already settled another invoice.")
    out = f"✅ Settled {_code(request_id[:8])}."
    note = _invoicing_off_note()
    out += (note if note else
            "\nThe settlement watcher will wake the originating session.")
    return out
